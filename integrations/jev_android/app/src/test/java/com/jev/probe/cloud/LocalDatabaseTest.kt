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
import java.net.ServerSocket
import java.net.InetAddress
import java.util.concurrent.atomic.AtomicReference
import kotlin.concurrent.thread

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

    @Test fun analysisDeletionScrubsJobResultsAndArchivedRecoveryCopies() {
        val db = database(); val secret = "删除后不可恢复的分析私密正文"; val selected = "删除后不可恢复的候选正文"
        val activeKey = "active|$selected"; val archivedKey = "archived|$selected"
        val result = JSONObject().put("analysis_id", "a").put("text", secret)
        val live = record(uid = "a").put("entity_type", "analyses").put("payload", JSONObject().put("result", result))
        db.applyPage(page(live))
        val activeBody = JSONObject().put("request_id", "active-request").put("conversation_uid", "c").put("kind", "rewrite").put("selected_text", selected)
        val archivedBody = JSONObject().put("request_id", "archived-request").put("conversation_uid", "c").put("kind", "rewrite").put("selected_text", selected)
        db.saveJob(activeKey, activeBody, "a", result)
        db.saveJob(archivedKey, archivedBody, "a", result)
        db.allowNewJob(archivedKey)
        val nextRequest = db.job(archivedKey)!!.getJSONObject("body").getString("request_id")
        db.saveJob("unrelated", JSONObject().put("request_id", "unrelated-request"), "other", JSONObject().put("text", "另一条结果"))

        db.applyPage(page(record(uid = "a", version = 2, deleted = true).put("entity_type", "analyses").put("payload", JSONObject()), "deleted"))

        val active = db.job(activeKey)!!
        assertTrue(active.isNull("result")); assertEquals("a", active.getString("analysis_id"))
        assertTrue(active.getBoolean("local_cancelled")); assertFalse(active.getJSONObject("body").has("selected_text"))
        assertEquals("active-request", active.getJSONObject("body").getString("request_id"))
        assertEquals(nextRequest, db.job(archivedKey)!!.getJSONObject("body").getString("request_id"))
        assertTrue(db.job(archivedKey)!!.getBoolean("local_cancelled"))
        assertThrows(IllegalStateException::class.java) { db.allowNewJob(activeKey) }
        assertThrows(IllegalStateException::class.java) { db.allowNewJob(archivedKey) }
        assertEquals("另一条结果", db.job("unrelated")!!.getJSONObject("result").getString("text"))
        assertEquals(3, db.jobs().size)
        val exported = db.exportRecovery(File(context.cacheDir, UUID.randomUUID().toString())).readText()
        assertFalse("Deleted analysis text must not survive in recovery JSON", exported.contains(secret))
        assertFalse("Deleted selected_text must not survive in keys, bodies or archived copies", exported.contains(selected))
        val metadata = JSONObject(exported).getJSONArray("meta")
        val archived = (0 until metadata.length()).map { metadata.getJSONObject(it) }
            .single { it.getString("k").startsWith("previous_job/") }.getString("v").let(::JSONObject)
        assertTrue(archived.isNull("result")); assertEquals("a", archived.getString("analysis_id"))
        assertTrue(archived.getBoolean("local_cancelled")); assertFalse(archived.getJSONObject("body").has("selected_text"))
        assertEquals("archived-request", archived.getJSONObject("body").getString("request_id"))
    }

    @Test fun lateJobResultAndOldPullCannotReviveDeletedAnalysis() {
        val db = database(); val body = JSONObject().put("request_id", "keep-this-request")
        val result = JSONObject().put("analysis_id", "a").put("text", "晚到的已删除私密正文")
        db.saveJob("active", body, "a")
        db.applyPage(page(record(uid = "a", version = 2, deleted = true).put("entity_type", "analyses").put("payload", JSONObject()), "deleted"))
        assertThrows(IllegalStateException::class.java) { db.saveJob("active", body, "a", result) }
        assertThrows(IllegalStateException::class.java) { db.saveJob("active", JSONObject().put("request_id", "must-not-replace-original").put("selected_text", "晚到的已删除私密正文"), "a", result) }
        assertThrows(IllegalStateException::class.java) { db.saveJob("new-key", JSONObject().put("request_id", "late-new-request").put("selected_text", "晚到的已删除私密正文"), "a", result) }
        db.applyPage(page(record(uid = "a", version = 1).put("entity_type", "analyses").put("payload", JSONObject().put("result", result)), "old-page"))
        assertEquals(2, db.record("analyses", "a")!!.getInt("version"))
        assertFalse(db.record("analyses", "a")!!.isNull("deleted_at"))
        assertTrue(db.job("active")!!.isNull("result")); assertEquals("a", db.job("active")!!.getString("analysis_id"))
        assertEquals("keep-this-request", db.job("active")!!.getJSONObject("body").getString("request_id"))
        assertEquals(2, db.jobs().size)
        assertTrue(db.job("new-key")!!.getBoolean("local_cancelled"))
        assertFalse(db.exportRecovery(File(context.cacheDir, UUID.randomUUID().toString())).readText().contains("晚到的已删除私密正文"))
    }

    @Test fun reopeningLegacyTombstoneScrubsPreviouslyCachedPrivateJobs() {
        val db = database(); val secret = "升级前残留的已删除结果和候选正文"
        db.applyPage(page(record(uid = "a", version = 2, deleted = true).put("entity_type", "analyses").put("payload", JSONObject())))
        val body = JSONObject().put("request_id", "legacy-request").put("conversation_uid", "c").put("selected_text", secret)
        val result = JSONObject().put("analysis_id", "a").put("text", secret)
        db.transaction { sql -> sql.execSQL("INSERT INTO jobs(k,body,analysis_id,result) VALUES(?,?,?,?)", arrayOf("legacy|$secret", body.toString(), "a", result.toString())) }
        db.setMeta("previous_job/legacy", JSONObject().put("body", body).put("analysis_id", "a").put("result", result).toString())
        db.close()
        val reopened = db.job("legacy|$secret")!!
        assertTrue(reopened.getBoolean("local_cancelled")); assertTrue(reopened.isNull("result"))
        assertEquals("legacy-request", reopened.getJSONObject("body").getString("request_id"))
        assertFalse(db.exportRecovery(File(context.cacheDir, UUID.randomUUID().toString())).readText().contains(secret))
    }

    @Test fun cancelledJobCannotReturnTextOrCreateAnotherPaidRequest() {
        val session = CloudSession("http://127.0.0.1:18000", "t", UUID.randomUUID().toString(), "e", "d", "s", "epoch")
        val db = LocalDatabase.get(context, session.accountKey)
        db.applyPage(page(record(uid = "c").put("entity_type", "conversations").put("payload", JSONObject().put("context_revision", 2))))
        val key = "c|2|explain|parent||"
        val body = JSONObject().put("request_id", "cancelled-request").put("conversation_uid", "c").put("context_revision", 2).put("kind", "explain").put("parent_analysis_id", "parent")
        db.saveJob(key, body, "a", JSONObject().put("text", "已删除任务结果"))
        db.applyPage(page(record(uid = "a", version = 2, deleted = true).put("entity_type", "analyses").put("payload", JSONObject())))
        val analysis = CloudProtocol.analysis(JSONObject("""{"schema_version":1,"analysis_id":"parent","conversation_uid":"c","context_revision":2,"candidates":[],"ranking_status":"unavailable"}"""))
            .copy(cloudAccount = session.accountKey, cloudDeviceId = session.deviceId)
        val server = ServerSocket(18000, 5, InetAddress.getByName("127.0.0.1")); val unexpected = AtomicReference<String?>()
        val serving = thread(isDaemon = true) { try { server.accept().use { socket ->
            unexpected.set(socket.getInputStream().bufferedReader().readLine())
            val response = "{\"error\":{\"code\":\"unexpected_request\"}}"
            socket.getOutputStream().write(("HTTP/1.1 400 Test\r\nContent-Length: ${response.length}\r\nConnection: close\r\n\r\n$response").toByteArray())
        } } catch (_: Exception) { } }
        try {
            assertThrows(IllegalStateException::class.java) { CloudAnalysis(context, object : CloudSessionAccess { override fun current() = session }).followup(analysis, "explain") }
            assertThrows(IllegalStateException::class.java) { db.allowNewJob(key) }
            assertNull(unexpected.get()); assertEquals("cancelled-request", db.job(key)!!.getJSONObject("body").getString("request_id"))
            assertEquals(1, db.jobs().size)
        } finally { server.close(); serving.join(1000) }
    }
}
