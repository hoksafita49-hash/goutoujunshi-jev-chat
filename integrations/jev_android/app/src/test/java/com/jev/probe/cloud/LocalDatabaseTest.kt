package com.jev.probe.cloud

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import com.jev.probe.data.LocalDatabase
import com.jev.probe.sync.LegacyImporter
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.File
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [30], application = android.app.Application::class)
class LocalDatabaseTest {
    private val context: Context get() = ApplicationProvider.getApplicationContext()
    private fun database() = LocalDatabase.get(context, "test-${UUID.randomUUID()}")
    private fun record(uid: String = "p", version: Int = 1, name: String = "云端", deleted: Boolean = false) =
        JSONObject().put("entity_type", "profiles").put("uid", uid).put("version", version)
            .put("created_at", "2026-10-21T00:00:00Z").put("updated_at", "2026-10-21T00:00:00Z")
            .put("deleted_at", if (deleted) "2026-10-21T00:00:00Z" else JSONObject.NULL).put("payload", JSONObject().put("name", name))
    private fun page(record: JSONObject, cursor: String = "page-1") = JSONObject().put("changes", JSONArray()
        .put(JSONObject().put("seq", 1).put("record", record))).put("cursor", cursor)

    @Test fun lostPushResponseReusesIdenticalOperationAndKeepsCursorIndependent() {
        val db = database(); db.edit("profiles", "p", JSONObject().put("name", "本地"))
        val first = db.batch().getJSONObject(0)
        assertEquals(first.toString(), db.batch().getJSONObject(0).toString())
        db.receipts(JSONArray().put(JSONObject().put("op_id", first.getString("op_id")).put("status", "applied")
            .put("record", record(name = "本地")).put("seq", 99)))
        assertEquals(0, db.pendingCount()); assertNull(db.meta("cursor"))
        assertEquals("本地", db.record("profiles", "p")!!.getJSONObject("payload").getString("name"))
    }
    @Test fun sharedSyncPageFixtureAppliesTombstoneAndCursorTogether() {
        val fixture = JSONObject(checkNotNull(javaClass.classLoader!!.getResourceAsStream("cloud-v1-sync-page.json")).bufferedReader().use { it.readText() })
        val db = database(); db.applyPage(fixture)
        assertEquals("opaque-test-cursor", db.meta("cursor"))
        assertEquals("示例人物", db.records("profiles").single().getJSONObject("payload").getString("name"))
        assertTrue(db.records("notes").isEmpty())
    }
    @Test fun editDuringAmbiguousPushDoesNotMutateSentOperationAndRebasesNextEditAfterReceipt() {
        val db = database(); db.edit("profiles", "p", JSONObject().put("name", "第一版"))
        val sent = db.batch().getJSONObject(0)
        db.edit("profiles", "p", JSONObject().put("name", "第二版"))
        assertEquals(sent.toString(), db.batch().getJSONObject(0).toString())
        db.receipts(JSONArray().put(JSONObject().put("op_id", sent.getString("op_id")).put("status", "applied").put("record", record(name = "第一版"))))
        val next = db.batch().getJSONObject(0)
        assertEquals(1, next.getInt("base_version")); assertEquals("第二版", next.getJSONObject("payload").getString("name"))
        assertEquals("第二版", db.record("profiles", "p")!!.getJSONObject("payload").getString("name"))
    }
    @Test fun epochRecoveryPreservesOutboxAndBlocksEditsUntilExplicitRebuild() {
        val db = database(); db.edit("profiles", "p", JSONObject().put("name", "未上传"))
        val op = db.batch().getJSONObject(0).getString("op_id")
        val directory = File(context.cacheDir, UUID.randomUUID().toString())
        val recovery = db.exportRecovery(directory)
        db.setMeta("recovery_required", "true")
        assertTrue(recovery.readText().contains(op)); assertEquals(1, db.pendingCount())
        assertThrows(IllegalStateException::class.java) { db.edit("profiles", "p", JSONObject().put("name", "不应写入")) }
        db.acceptRebuild(directory)
        assertEquals(0, db.pendingCount()); assertTrue(directory.listFiles()!!.isNotEmpty())
    }
    @Test fun pullingRemoteVersionPreservesLocalEditAndAdvancesCursorAtomically() {
        val db = database(); db.applyPage(page(record()))
        db.edit("profiles", "p", JSONObject().put("name", "待上传"))
        db.applyPage(page(record(version = 2, name = "另一台设备"), "page-2"))
        assertEquals("待上传", db.record("profiles", "p")!!.getJSONObject("payload").getString("name"))
        assertEquals("page-2", db.meta("cursor")); assertEquals(1, db.pendingCount())
        val invalid = page(record(version = 3), "bad").apply {
            getJSONArray("changes").put(JSONObject().put("record", JSONObject()))
        }
        assertThrows(Exception::class.java) { db.applyPage(invalid) }
        assertEquals("page-2", db.meta("cursor"))
    }
    @Test fun historicalPullVersionsCannotRollBackMirrorOrBlockANewEdit() {
        val db = database(); db.applyPage(page(record(version = 3, name = "当前"), "current"))
        db.edit("profiles", "p", JSONObject().put("name", "新编辑"))
        db.applyPage(page(record(version = 1), "old-1")); db.applyPage(page(record(version = 2), "old-2"))
        assertEquals("新编辑", db.record("profiles", "p")!!.getJSONObject("payload").getString("name"))
        assertEquals(3, db.batch().getJSONObject(0).getInt("base_version"))
        assertTrue(db.conflicts().isEmpty())
    }
    @Test fun conflictKeepsBothVersionsUntilExplicitChoice() {
        val db = database(); db.edit("profiles", "p", JSONObject().put("name", "本地"))
        val op = db.batch().getJSONObject(0).getString("op_id")
        db.receipts(JSONArray().put(JSONObject().put("op_id", op).put("status", "conflict").put("record", record(name = "云端"))))
        assertEquals(1, db.pendingCount()); assertEquals(1, db.conflicts().size); assertEquals(0, db.batch().length())
        assertEquals("本地", db.conflicts()[0].getJSONObject("local").getJSONObject("payload").getString("name"))
        assertEquals("云端", db.conflicts()[0].getJSONObject("remote").getJSONObject("payload").getString("name"))
        db.resolve(op, true)
        val next = db.batch().getJSONObject(0)
        assertNotEquals(op, next.getString("op_id")); assertEquals(1, next.getInt("base_version"))
    }
    @Test fun accountsAreIsolatedAndRemoteTombstoneHidesDirtyObject() {
        val a = database(); val b = database()
        a.edit("profiles", "p", JSONObject().put("name", "私人资料"))
        assertTrue(b.records("profiles").isEmpty()); assertEquals(0, b.pendingCount())
        a.applyPage(page(record(deleted = true)))
        assertTrue(a.records("profiles").isEmpty())
        assertEquals(1, a.pendingCount()) // preserved for conflict/recovery, never resurrected
    }
    @Test fun repeatedLegacyImportPreservesRepeatedShortMessagesAndSourceFiles() {
        val root = File(context.filesDir, "kb").apply { mkdirs() }
        File(root, "contacts.json").writeText("""[{"id":"old","name":"甲","apps":["chat"],"aliases":[],"relationship":"朋友"}]""")
        File(root, "notes.json").writeText("""[{"id":"n","title":"知识","content":"正文","alwaysOn":true}]""")
        File(root, "logs").mkdirs()
        val source = File(root, "logs/old.json").apply { writeText("""[{"side":"other","text":"好","ts":1000},{"side":"other","text":"好","ts":1000}]""") }
        val before = source.readText()
        val session = CloudSession("https://example.com", "t", UUID.randomUUID().toString(), "e", "d", "s", "epoch")
        val importer = LegacyImporter(context, session)
        assertTrue(importer.preview().contains("other: 好")); importer.importConfirmed(); importer.importConfirmed()
        val db = LocalDatabase.get(context, session.accountKey)
        assertEquals(1, db.records("profiles").size); assertEquals(2, db.records("messages").size)
        assertEquals("1970-01-01T00:00:01Z", db.records("messages")[0].getJSONObject("payload").getString("observed_at"))
        assertEquals(before, source.readText())
        assertTrue(db.records("notes")[0].getJSONObject("payload").getBoolean("always_on"))
    }
}
