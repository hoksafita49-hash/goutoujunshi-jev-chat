package com.jev.probe.cloud

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import com.jev.probe.data.LocalDatabase
import com.jev.probe.data.ContactRepository
import com.jev.probe.core.kb.Contact
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.util.UUID
import java.util.concurrent.CancellationException
import java.util.concurrent.atomic.AtomicReference
import kotlin.concurrent.thread

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [30], application = android.app.Application::class)
class EvidenceMemoryTest {
    private val context: Context get() = ApplicationProvider.getApplicationContext()
    private fun account() = CloudSession("https://example.com", "token", UUID.randomUUID().toString(), "test", "device", "secret", "epoch")
    private fun record(type: String, uid: String, version: Int, payload: JSONObject) = JSONObject()
        .put("entity_type", type).put("uid", uid).put("version", version).put("payload", payload)
        .put("created_at", "2026-10-21T00:00:00Z").put("updated_at", "2026-10-21T01:00:00Z").put("deleted_at", JSONObject.NULL)
    private fun mirror(db: LocalDatabase, vararg records: JSONObject) = db.applyPage(JSONObject().put("cursor", "1")
        .put("changes", JSONArray(records.map { JSONObject().put("record", it) })))
    private fun repository(session: CloudSession, read: (CloudSession, String) -> JSONObject = { _, _ -> error("Unexpected network") }): EvidenceMemoryRepository {
        val binding = CloudAccountBinding.capture(object : CloudSessionAccess { override fun current() = session })
        return EvidenceMemoryRepository(LocalDatabase.get(context, session.accountKey), binding, read)
    }
    private fun memory(profile: String = "p") = JSONObject().put("profile_uid", profile).put("text", "旧事实")
        .put("kind", "fact").put("status", "confirmed").put("source_uids", JSONArray(listOf("m")))
        .put("source_refs", JSONArray(listOf(JSONObject().put("message_uid", "m").put("version", 3))))
        .put("source_type", "messages").put("change_reason", "首次核对").put("future_field", "保留")

    @Test fun prefsKeepUnknownAndSendCompleteOverrides() {
        val original = JSONObject().put("tone", "轻松").put("length", "短").put("count", 2).put("style", "旧习惯")
            .put("future", JSONObject().put("flag", true))
        val saved = EvidenceMemoryPreferences.save(original, "自然", "适中", 9, "")
        assertTrue(saved.getJSONObject("future").getBoolean("flag"))
        assertEquals(3, saved.getInt("count")); assertEquals("", saved.getString("style"))
        assertEquals("旧习惯", original.getString("style"))
        val request = EvidenceMemoryPreferences.request(JSONObject().put("style", "保留习惯").put("future", 7))
        assertEquals("自然", request.getString("tone")); assertEquals("适中", request.getString("length"))
        assertEquals(3, request.getInt("count")); assertEquals("保留习惯", request.getString("style")); assertEquals(7, request.getInt("future"))
    }

    @Test fun manualAndClaimStartPending() {
        val session = account(); val db = LocalDatabase.get(context, session.accountKey); val repo = repository(session)
        mirror(db, record("profiles", "p", 1, JSONObject().put("name", "甲")),
            record("messages", "m", 3, JSONObject().put("profile_uid", "p").put("confirmed", true).put("text", "快照原话")))
        val manual = repo.create("p", "手动说法", "hypothesis")
        assertEquals("pending", manual.getJSONObject("payload").getString("status"))
        assertEquals("user", manual.getJSONObject("payload").getString("source_type"))
        val claim = EvidenceClaim("c", "fact", "快照事实", listOf(EvidenceQuote("m", "原话", "other", "2026-10-21T00:00:00Z")))
        val fromClaim = repo.fromClaim("p", claim)
        assertEquals("pending", fromClaim.getJSONObject("payload").getString("status"))
        assertEquals("m", fromClaim.getJSONObject("payload").getJSONArray("source_uids").getString(0))
        assertThrows(IllegalArgumentException::class.java) { repo.update(manual, "手动说法", "hypothesis", "confirmed", "确认") }
        assertEquals(2, db.pendingCount())
    }

    @Test fun editsPreserveSourcesAndRequireCurrentVersion() {
        val session = account(); val db = LocalDatabase.get(context, session.accountKey); val repo = repository(session)
        mirror(db, record("memories", "u", 7, memory()))
        val visible = db.record("memories", "u")!!
        repo.update(visible, "改正事实", "fact", "pending", "核对原话后更正")
        val edited = db.record("memories", "u")!!.getJSONObject("payload")
        assertEquals("m", edited.getJSONArray("source_uids").getString(0)); assertEquals(3, edited.getJSONArray("source_refs").getJSONObject(0).getInt("version"))
        assertEquals("保留", edited.getString("future_field")); assertEquals("核对原话后更正", edited.getString("change_reason"))
        assertThrows(IllegalStateException::class.java) { repo.update(visible, "覆盖草稿", "fact", "pending", "过期界面") }
        assertEquals("改正事实", db.record("memories", "u")!!.getJSONObject("payload").getString("text"))
    }

    @Test fun restoreUsesCurrentVersionAndNeverConfirms() {
        val session = account(); val db = LocalDatabase.get(context, session.accountKey); val repo = repository(session)
        mirror(db, record("memories", "u", 7, memory().put("text", "当前正文")))
        val old = record("memories", "u", 2, memory().put("text", "历史正文").put("status", "confirmed"))
        repo.restore(db.record("memories", "u")!!, old)
        val op = db.batch().getJSONObject(0)
        assertEquals(7, op.getInt("base_version")); assertEquals("历史正文", op.getJSONObject("payload").getString("text"))
        assertEquals("pending", op.getJSONObject("payload").getString("status"))
        assertEquals("恢复版本 2", op.getJSONObject("payload").getString("change_reason"))
        assertEquals(7, db.record("memories", "u")!!.getInt("version"))
    }

    @Test fun evidenceUsesExactSnapshotAndMarksLegacyUnavailable() {
        val raw = JSONObject("""{"analysis_id":"a","conversation_uid":"c","profile_uid":"p","context_revision":2,"evidence":[{"claim_id":"ok","kind":"fact","text":"有原话","source_message_uids":["m"],"source_quotes":[{"message_uid":"m","quote":"原话"}],"citation_status":"verified"},{"claim_id":"bad","kind":"hypothesis","text":"猜测","source_message_uids":["m"],"source_quotes":[{"message_uid":"m","quote":"伪造"}],"citation_status":"verified"}],"messages":[{"uid":"m","version":3,"role":"other","text":"快照原话","observed_at":"2026-10-21T00:00:00Z","order_key":"1"}]}""")
        val view = EvidenceSnapshot.read(raw, JSONObject())
        assertEquals("原话", view.claims[0].quotes.single().quote)
        assertEquals("other", view.claims[0].quotes.single().speaker); assertEquals("2026-10-21T00:00:00Z", view.claims[0].quotes.single().observedAt)
        assertEquals("快照原话", view.claims[0].quotes.single().originalText)
        assertEquals(3, view.claims[0].quotes.single().version)
        assertTrue(view.claims[1].quotes.isEmpty())
        raw.put("evidence", JSONArray())
        val legacy = EvidenceSnapshot.read(raw, JSONObject().put("facts", JSONArray(listOf("旧事实"))))
        assertEquals("旧事实", legacy.claims.single().text); assertTrue(legacy.claims.single().quotes.isEmpty())
    }

    @Test fun historyPagesAndLateResponsesStayInCapturedAccount() {
        val session = account(); val active = AtomicReference(session); val db = LocalDatabase.get(context, session.accountKey)
        mirror(db, record("memories", "u", 7, memory()))
        val binding = CloudAccountBinding.capture(object : CloudSessionAccess { override fun current() = active.get() })
        val paths = mutableListOf<String>()
        val repo = EvidenceMemoryRepository(db, binding) { used, path ->
            assertEquals(session.accountKey, used.accountKey); paths.add(path)
            val older = path.contains("before_version=2")
            JSONObject().put("memory_uid", "u").put("profile_uid", "p").put("versions", JSONArray(listOf(record("memories", "u", if (older) 1 else 2, memory()))))
                .put("has_more", !older).put("next_before_version", if (older) 0 else 2)
        }
        val page = repo.history("u", 0)
        assertEquals(2, page.nextBeforeVersion); assertTrue(page.hasMore)
        assertEquals(1, repo.history("u", page.nextBeforeVersion).versions.single().getInt("version"))
        assertEquals(listOf("/v1/memories/u/history?before_version=0&limit=50", "/v1/memories/u/history?before_version=2&limit=50"), paths)
        val late = EvidenceMemoryRepository(db, binding) { _, _ ->
            active.set(account()); JSONObject().put("memory_uid", "u").put("profile_uid", "p").put("versions", JSONArray())
        }
        assertThrows(CancellationException::class.java) { late.history("u") }
        assertThrows(CancellationException::class.java) { repo.create("p", "晚到内容", "fact") }
        assertEquals(0, db.pendingCount())
    }

    @Test fun changedTextOrKindCannotConfirmInTheSameEdit() {
        val session = account(); val db = LocalDatabase.get(context, session.accountKey); val repo = repository(session)
        mirror(db, record("memories", "u", 7, memory().put("kind", "hypothesis").put("status", "pending")))
        val converted = repo.update(db.record("memories", "u")!!, "有依据的新事实", "fact", "confirmed", "明确转为事实")
        assertEquals("pending", converted.getJSONObject("payload").getString("status"))
        assertThrows(IllegalStateException::class.java) { repo.update(converted, "有依据的新事实", "fact", "confirmed", "尚未同步就确认") }
        val staged = db.batch().getJSONObject(0)
        assertEquals("pending", staged.getJSONObject("payload").getString("status"))
        db.receipts(JSONArray(listOf(JSONObject().put("op_id", staged.getString("op_id")).put("status", "applied")
            .put("record", record("memories", "u", 8, converted.getJSONObject("payload"))))))
        val confirmed = repo.update(db.record("memories", "u")!!, "有依据的新事实", "fact", "confirmed", "单独核对后确认")
        assertEquals("confirmed", confirmed.getJSONObject("payload").getString("status"))
        assertTrue(db.hasLocalChanges("memories", "u"))
        val corrected = repo.update(confirmed, "再更正文", "fact", "confirmed", "纠正")
        assertEquals("pending", corrected.getJSONObject("payload").getString("status"))
    }

    @Test fun foreignAndDeletedSourcesCannotCreateMemory() {
        val session = account(); val db = LocalDatabase.get(context, session.accountKey); val repo = repository(session)
        mirror(db, record("profiles", "p", 1, JSONObject().put("name", "甲")),
            record("messages", "foreign", 1, JSONObject().put("profile_uid", "other-person").put("confirmed", true)),
            record("messages", "deleted", 2, JSONObject().put("profile_uid", "p").put("confirmed", true)).put("deleted_at", "2026-10-21T03:00:00Z"))
        for (uid in listOf("foreign", "deleted")) assertThrows(IllegalArgumentException::class.java) {
            repo.fromClaim("p", EvidenceClaim("c", "fact", "不应写入", listOf(EvidenceQuote(uid, "原话", "other", ""))))
        }
        assertTrue(db.records("memories").isEmpty()); assertEquals(0, db.pendingCount())
    }

    @Test fun deletingRequiresConfirmationAndCannotRestoreTombstone() {
        val session = account(); val db = LocalDatabase.get(context, session.accountKey); val repo = repository(session)
        val old = record("memories", "u", 7, memory()); mirror(db, old)
        assertThrows(IllegalArgumentException::class.java) { repo.delete(old, false) }
        assertTrue(db.records("memories").isNotEmpty())
        repo.delete(old, true)
        assertTrue(db.records("memories").isEmpty())
        assertThrows(IllegalStateException::class.java) { repo.restore(old, old) }
        assertEquals("delete", db.batch().getJSONObject(0).getString("action"))
    }

    @Test fun sentMemoryBodyStaysImmutableAndNextEditUsesReceiptVersion() {
        val session = account(); val db = LocalDatabase.get(context, session.accountKey); val repo = repository(session)
        mirror(db, record("memories", "u", 7, memory()))
        val first = repo.update(db.record("memories", "u")!!, "第一次修正", "fact", "pending", "修正一")
        val sent = db.batch().getJSONObject(0)
        repo.update(first, "第二次修正", "fact", "pending", "修正二")
        assertEquals(2, db.pendingCount())
        val retry = db.batch().getJSONObject(0)
        assertEquals(sent.getString("op_id"), retry.getString("op_id"))
        assertEquals("第一次修正", retry.getJSONObject("payload").getString("text"))
        db.receipts(JSONArray(listOf(JSONObject().put("op_id", sent.getString("op_id")).put("status", "applied")
            .put("record", record("memories", "u", 8, first.getJSONObject("payload"))))))
        val next = db.batch().getJSONObject(0)
        assertEquals(8, next.getInt("base_version")); assertEquals("第二次修正", next.getJSONObject("payload").getString("text"))
        assertEquals("m", next.getJSONObject("payload").getJSONArray("source_uids").getString(0))
    }

    @Test fun evidenceReadUsesExpectedContextAndRejectsLateAccountResponse() {
        val session = account(); val active = AtomicReference(session); val db = LocalDatabase.get(context, session.accountKey)
        val analysis = record("analyses", "a", 1, JSONObject().put("profile_uid", "p").put("conversation_uid", "c").put("context_revision", 2)
            .put("result", JSONObject().put("facts", JSONArray(listOf("旧事实")))))
        mirror(db, analysis, record("conversations", "c", 1, JSONObject().put("profile_uid", "p").put("context_revision", 2)))
        val raw = JSONObject().put("analysis_id", "a").put("profile_uid", "p").put("conversation_uid", "c").put("context_revision", 2)
            .put("evidence", JSONArray()).put("messages", JSONArray())
        val binding = CloudAccountBinding.capture(object : CloudSessionAccess { override fun current() = active.get() })
        val repo = EvidenceMemoryRepository(db, binding) { used, path ->
            assertEquals(session.accountKey, used.accountKey); assertEquals("/v1/analyses/a/evidence", path); JSONObject(raw.toString())
        }
        assertEquals("旧事实", repo.evidence(analysis).claims.single().text)
        val legacy = JSONObject(analysis.toString()).apply { getJSONObject("payload").remove("profile_uid") }
        assertEquals("旧事实", repo.evidence(legacy).claims.single().text)
        raw.put("profile_uid", "foreign")
        assertThrows(IllegalArgumentException::class.java) { repo.evidence(analysis) }
        val late = EvidenceMemoryRepository(db, binding) { _, _ -> active.set(account()); raw }
        assertThrows(CancellationException::class.java) { late.evidence(analysis) }
    }

    @Test fun oldJobKeyStillReturnsSavedResultWithoutAnotherPost() {
        val session = account().copy(baseUrl = "http://127.0.0.1:18000")
        val db = LocalDatabase.get(context, session.accountKey)
        ContactRepository(context, session).saveContact(Contact("p", "人物", listOf("聊天"), listOf("chat")))
        mirror(db, record("conversations", "c", 1, JSONObject().put("profile_uid", "p").put("title", "聊天").put("platform", "chat").put("context_revision", 2)))
        db.saveJob("c|2|explain|a||", JSONObject().put("request_id", "saved-id"), "existing", JSONObject().put("kept", true))
        val analysis = CloudProtocol.analysis(JSONObject("""{"schema_version":1,"analysis_id":"a","conversation_uid":"c","context_revision":2,"candidates":[],"ranking_status":"unavailable"}"""))
            .copy(cloudAccount = session.accountKey, cloudDeviceId = session.deviceId, cloudProfileUid = "p")
        val server = LoopbackGatewayTestServer()
        val unexpected = AtomicReference<String?>()
        val serving = thread(isDaemon = true) { try {
            server.accept().use { socket ->
                unexpected.set(socket.getInputStream().bufferedReader().readLine())
                val body = "{\"error\":{\"code\":\"unexpected_new_job\"}}"
                socket.getOutputStream().write(("HTTP/1.1 400 Test\r\nContent-Length: ${body.length}\r\nConnection: close\r\n\r\n$body").toByteArray())
            }
        } catch (_: Exception) { } }
        try {
            val service = CloudAnalysis(context, object : CloudSessionAccess { override fun current() = session })
            val result = runCatching { service.followup(analysis, "explain") }.getOrNull()
            assertTrue("Saved request/result must survive preference normalization; unexpected request=${unexpected.get()}", result?.optBoolean("kept") == true)
            assertNull(unexpected.get())
            assertEquals(1, db.jobs().size)
        } finally { server.close(); serving.join(1000) }
    }
}
