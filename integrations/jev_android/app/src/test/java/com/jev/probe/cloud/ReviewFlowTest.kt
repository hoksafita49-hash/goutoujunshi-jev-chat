package com.jev.probe.cloud

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import com.jev.probe.core.ChatSnapshot
import com.jev.probe.core.Analysis
import com.jev.probe.core.WorkScope
import com.jev.probe.core.WorkToken
import com.jev.probe.core.Prefs
import com.jev.probe.core.Msg
import com.jev.probe.core.kb.Contact
import com.jev.probe.data.ContactRepository
import org.json.JSONObject
import org.json.JSONArray
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.util.UUID
import java.util.concurrent.CancellationException

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [30], application = android.app.Application::class)
class ReviewFlowTest {
    private val context: Context get() = ApplicationProvider.getApplicationContext()
    private fun account() = CloudSession("https://example.com", "token", UUID.randomUUID().toString(), "test", "device", "secret", "epoch")

    @Test fun unreliableTitleCannotReuseAnOldPlaceholderBinding() {
        val account = account()
        val repo = ContactRepository(context, account)
        val old = Contact(UUID.randomUUID().toString(), "旧人物", listOf("已确认对话"), listOf("chat"))
        repo.saveContact(old)
        val binding = CloudAccountBinding.capture(object : CloudSessionAccess { override fun current() = account })
        val snapshot = ChatSnapshot(null, listOf(Msg("other", "另一人的原文")))

        assertThrows(IllegalStateException::class.java) { repo.confirm(snapshot, "chat", snapshot, binding) }
        assertTrue(repo.db.records("messages").isEmpty())
    }

    @Test fun applicationNamesAndLoadingPlaceholdersCannotIdentifyPeople() {
        for (title in listOf("未识别会话", "未识别到会话", "尚未识别到会话", "未知会话", "未命名会话", "未命名",
            "微信", "wechat", "WeChAt", "QQ", "qq", "unknown", "untitled", "抖音", "douyin", "DOUYIN",
            "当前对话", "聊天记录", "未知", "未识别", " Connecting… ", "正在加载中…", "同步中...", "")) {
            assertFalse("must require an active person choice: $title", ReviewTitle.reliable(title))
        }
        assertTrue(ReviewTitle.reliable("小王"))
    }

    @Test fun spacedReliableTitlesMatchLegacyBindingsAndCanBeReviewedTwice() {
        val account = account(); val repo = ContactRepository(context, account)
        val person = Contact(UUID.randomUUID().toString(), "人物", listOf(" 聊天 "), listOf("chat")); repo.saveContact(person)
        val legacyLink = repo.db.records("identity_links").single { it.getJSONObject("payload").getString("legacy_id") == "聊天" }
        repo.db.edit("identity_links", legacyLink.getString("uid"), JSONObject(legacyLink.getJSONObject("payload").toString()).put("legacy_id", " 聊天 "))
        val binding = CloudAccountBinding.capture(object : CloudSessionAccess { override fun current() = account })
        val snapshot = ChatSnapshot(" 聊天 ", listOf(Msg("other", "原文")))
        repeat(2) { round ->
            val before = repo.findContact(snapshot.title!!, "chat")!!.id
            val pair = repo.confirm(snapshot, "chat", snapshot, binding, before, ReviewPersonChoice(profileUid = person.id), "space-$round")
            assertEquals(person.id, pair.first)
        }
        assertEquals(1, repo.db.records("identity_links").count { it.getJSONObject("payload").getString("legacy_id").trim() == "聊天" })
        assertEquals(person.id, repo.findContact("聊天", "chat")!!.id)
    }

    @Test fun upgradedRawTitleKeepsTheOriginalConversationMessagesAndAmbiguousRequestBody() {
        val account = account(); val repo = ContactRepository(context, account)
        val person = UUID.randomUUID().toString()
        val rawTitle = " 聊天 "
        val conversation = repo.db.legacyUid("conversation/$person/chat/$rawTitle")
        val source = ChatSnapshot(rawTitle, listOf(Msg("other", "原来那一句"), Msg("me", "我还想聊")))
        val messageIds = listOf(UUID.randomUUID().toString(), UUID.randomUUID().toString())
        val sourceDevice = Prefs(context).installationId
        mirror(repo,
            remote("profiles", person, JSONObject().put("name", "旧人物")),
            remote("identity_links", UUID.randomUUID().toString(), JSONObject().put("profile_uid", person)
                .put("platform", "chat").put("legacy_id", rawTitle).put("source_device", sourceDevice)),
            remote("conversations", conversation, JSONObject().put("profile_uid", person).put("title", rawTitle)
                .put("platform", "chat").put("context_revision", 3)),
            *source.messages.mapIndexed { i, message -> remote("messages", messageIds[i], JSONObject()
                .put("profile_uid", person).put("conversation_uid", conversation).put("confirmed", true)
                .put("role", message.side).put("text", message.text).put("observed_at", "2026-10-21T00:00:00Z")
                .put("order_key", "old-$i")) }.toTypedArray())
        val keys = JSONArray(source.messages.map { "${it.side}\u0000${it.text}" }).toString()
        repo.db.setMeta("screen/$conversation", keys)
        repo.db.setMeta("screen_source/$conversation", keys)
        repo.db.setMeta("screen_rows/$conversation", JSONArray(messageIds).toString())
        val oldKey = "$conversation|3|analysis|||"
        val body = JSONObject().put("request_id", "original-unknown-request").put("conversation_uid", conversation)
            .put("context_revision", 3).put("kind", "analysis").put("preferences", JSONObject().put("style", "原请求偏好"))
        val originalBody = body.toString()
        repo.db.saveJob(oldKey, body) // The old POST outcome is unknown; this exact ID/body must survive.
        val binding = CloudAccountBinding.capture(object : CloudSessionAccess { override fun current() = account })

        val pair = repo.confirm(source, "chat", source, binding, person, ReviewPersonChoice(profileUid = person), "upgrade-review")

        assertEquals(conversation, pair.second)
        assertEquals(rawTitle, repo.db.record("conversations", conversation)!!.getJSONObject("payload").getString("title"))
        assertEquals(messageIds, repo.db.records("messages").map { it.getString("uid") })
        assertEquals(1, repo.db.records("conversations").size)
        assertEquals(1, repo.db.jobs().size)
        val retained = repo.db.job(CloudAnalysisRequest.key(pair.second, 3, "analysis", null, null, null, " "))!!
        assertEquals("original-unknown-request", retained.getJSONObject("body").getString("request_id"))
        assertEquals(originalBody, retained.getJSONObject("body").toString())
        assertTrue(repo.analysisCurrent(analysis(account, person, conversation).copy(contextRevision = 3), rawTitle, "chat"))
        val batch = repo.db.batch()
        assertTrue((0 until batch.length()).none { batch.getJSONObject(it).getString("entity_type") in listOf("messages", "conversations") })
    }

    @Test fun ambiguousCanonicalConversationsStopBeforeCreatingAnotherContextOrRequest() {
        val account = account(); val repo = ContactRepository(context, account)
        val person = Contact(UUID.randomUUID().toString(), "人物", listOf("聊天"), listOf("chat")); repo.saveContact(person)
        val conversationOne = UUID.randomUUID().toString(); val conversationTwo = UUID.randomUUID().toString()
        mirror(repo,
            remote("conversations", conversationOne, JSONObject().put("profile_uid", person.id).put("title", " 聊天 ").put("platform", "chat").put("context_revision", 1)),
            remote("conversations", conversationTwo, JSONObject().put("profile_uid", person.id).put("title", "聊天").put("platform", "chat").put("context_revision", 2)))
        val before = repo.db.pendingCount()
        val binding = CloudAccountBinding.capture(object : CloudSessionAccess { override fun current() = account })
        val source = ChatSnapshot("聊天", listOf(Msg("other", "本轮原文")))

        val failure = assertThrows(IllegalStateException::class.java) {
            repo.confirm(source, "chat", source, binding, person.id, ReviewPersonChoice(profileUid = person.id), "ambiguous-review")
        }
        assertTrue(failure.message.orEmpty().contains("恢复原任务"))
        assertEquals(before, repo.db.pendingCount())
        assertEquals(setOf(conversationOne, conversationTwo), repo.db.records("conversations").map { it.getString("uid") }.toSet())
        assertTrue(repo.db.records("messages").isEmpty())
        assertTrue(repo.db.jobs().isEmpty())
    }

    private fun remote(type: String, uid: String, payload: JSONObject) = JSONObject().put("entity_type", type)
        .put("uid", uid).put("version", 1).put("created_at", "2026-10-21T00:00:00Z")
        .put("updated_at", "2026-10-21T00:00:00Z").put("deleted_at", JSONObject.NULL).put("payload", payload)

    private fun mirror(repo: ContactRepository, vararg records: JSONObject) = repo.db.applyPage(JSONObject().put("cursor", "upgrade")
        .put("changes", JSONArray(records.map { JSONObject().put("record", it) })))

    @Test fun cancellingWhileWaitingForTheAccountLockDoesNotWriteConfirmedSelection() {
        val account = account(); val repo = ContactRepository(context, account)
        val token = WorkToken()
        val binding = CloudAccountBinding.capture(object : CloudSessionAccess { override fun current() = account })
        val snapshot = ChatSnapshot("新会话", listOf(Msg("other", "未提交原文")))
        assertThrows(CancellationException::class.java) {
            WorkScope.run(token) {
                // Work has started, then cancellation arrives before the short transaction can run.
                token.cancel()
                repo.confirm(snapshot, "chat", snapshot, binding, null, ReviewPersonChoice(newName = "未确认人物"), "cancelled")
            }
        }
        assertTrue(repo.contacts().isEmpty())
        assertTrue(repo.db.records("messages").isEmpty())
        assertTrue(repo.db.records("identity_links").isEmpty())
        assertTrue(repo.db.records("conversations").isEmpty())
        assertEquals(0, repo.db.pendingCount())
    }

    @Test fun explicitSelectionCanReplaceTheCurrentPersonWithoutMovingHistory() {
        val account = account(); val repo = ContactRepository(context, account)
        val a = Contact(UUID.randomUUID().toString(), "甲", listOf("聊天"), listOf("chat"))
        val b = Contact(UUID.randomUUID().toString(), "乙")
        repo.saveContact(a); repo.saveContact(b)
        val binding = CloudAccountBinding.capture(object : CloudSessionAccess { override fun current() = account })
        val first = ChatSnapshot("聊天", listOf(Msg("other", "甲的原文")))
        val original = repo.confirm(first, "chat", first, binding)
        val next = ChatSnapshot("聊天", listOf(Msg("other", "乙的原文")))
        val selected = repo.confirm(next, "chat", next, binding, a.id, ReviewPersonChoice(profileUid = b.id), "round-b")

        assertEquals(b.id, selected.first)
        assertEquals(b.id, repo.findContact("聊天", "chat")!!.id)
        assertNotEquals(original.second, selected.second)
        val previousMessage = repo.db.records("messages").single { it.getJSONObject("payload").getString("text") == "甲的原文" }.getJSONObject("payload")
        assertEquals(a.id, previousMessage.getString("profile_uid"))
        assertEquals(original.second, previousMessage.getString("conversation_uid"))
        repo.bindContact(b.id, "另一个名字", "another-chat-app")
        assertEquals(b.id, repo.findContact("另一个名字", "another-chat-app")!!.id)
    }

    @Test fun placeholderSelectionsArePerReviewAndNeverCreateIdentityLinks() {
        val account = account(); val repo = ContactRepository(context, account)
        val a = Contact(UUID.randomUUID().toString(), "共享人物"); repo.saveContact(a)
        val binding = CloudAccountBinding.capture(object : CloudSessionAccess { override fun current() = account })
        val first = ChatSnapshot("已确认对话", listOf(Msg("other", "第一轮")))
        val one = repo.confirm(first, "chat", first, binding, null, ReviewPersonChoice(profileUid = a.id), "round-one")
        val two = repo.confirm(first, "chat", first, binding, null, ReviewPersonChoice(profileUid = a.id), "round-two")
        assertNotEquals(one.second, two.second)
        assertTrue(repo.db.records("identity_links").isEmpty())
        assertNull(repo.findContact("已确认对话", "chat"))
        assertTrue(repo.analysisCurrent(analysis(account, a.id, one.second, "round-one"), null, "chat"))
        assertFalse(repo.analysisCurrent(analysis(account, a.id, one.second, "wrong-round"), null, "chat"))
        assertFalse(repo.analysisCurrent(analysis(account, a.id, one.second, "round-one"), null, "different-app"))
    }

    @Test fun explicitNewPersonDoesNotMergeEqualNamesOrPrecreateOnCancel() {
        val account = account(); val repo = ContactRepository(context, account)
        repo.saveContact(Contact(UUID.randomUUID().toString(), "同名"))
        val binding = CloudAccountBinding.capture(object : CloudSessionAccess { override fun current() = account })
        val snapshot = ChatSnapshot("新会话", listOf(Msg("other", "本轮原文")))
        val pair = repo.confirm(snapshot, "chat", snapshot, binding, null, ReviewPersonChoice(newName = " 同名 "), "round-new")
        assertEquals(2, repo.contacts().size)
        assertEquals("同名", repo.contact(pair.first)!!.name)
        assertEquals(pair.first, repo.findContact("新会话", "chat")!!.id)
        val before = repo.db.pendingCount()
        assertThrows(IllegalStateException::class.java) {
            repo.confirm(snapshot, "chat", snapshot, binding, pair.first, ReviewPersonChoice(newName = "  "), "bad")
        }
        assertEquals(before, repo.db.pendingCount())
    }

    @Test fun selectionRejectsBindingChangesAndDeletedPeopleBeforeAnyWrite() {
        val account = account(); val repo = ContactRepository(context, account)
        val a = Contact(UUID.randomUUID().toString(), "甲", listOf("聊天"), listOf("chat"))
        val b = Contact(UUID.randomUUID().toString(), "乙"); repo.saveContact(a); repo.saveContact(b)
        val binding = CloudAccountBinding.capture(object : CloudSessionAccess { override fun current() = account })
        val snapshot = ChatSnapshot("聊天", listOf(Msg("other", "原文")))
        repo.bindContact(b.id, "聊天", "chat")
        val before = repo.db.pendingCount()
        assertThrows(IllegalStateException::class.java) {
            repo.confirm(snapshot, "chat", snapshot, binding, a.id, ReviewPersonChoice(profileUid = b.id), "stale")
        }
        assertEquals(before, repo.db.pendingCount())
        repo.deleteContact(a.id)
        assertThrows(IllegalStateException::class.java) {
            repo.confirm(snapshot, "chat", snapshot, binding, b.id, ReviewPersonChoice(profileUid = a.id), "deleted")
        }
        assertTrue(repo.db.records("messages").isEmpty())
    }

    @Test fun currentAnalysisChecksTheConversationPersonAndCurrentBinding() {
        val account = account(); val repo = ContactRepository(context, account)
        val a = Contact(UUID.randomUUID().toString(), "甲", listOf("聊天"), listOf("chat"))
        val b = Contact(UUID.randomUUID().toString(), "乙"); repo.saveContact(a); repo.saveContact(b)
        val binding = CloudAccountBinding.capture(object : CloudSessionAccess { override fun current() = account })
        val snapshot = ChatSnapshot("聊天", listOf(Msg("other", "原文")))
        val pair = repo.confirm(snapshot, "chat", snapshot, binding)
        val current = analysis(account, a.id, pair.second)
        assertTrue(repo.analysisCurrent(current, "聊天", "chat"))
        assertFalse(repo.analysisCurrent(current.copy(cloudProfileUid = b.id), "聊天", "chat"))
        repo.bindContact(b.id, "聊天", "chat")
        assertFalse(repo.analysisCurrent(current, "聊天", "chat"))
    }

    private fun analysis(session: CloudSession, profile: String, conversation: String, review: String? = null) =
        Analysis(null, null, null, null, null, null, null, emptyList(), 0, conversationUid = conversation,
            contextRevision = 0, cloudAccount = session.accountKey, cloudProfileUid = profile,
            cloudDeviceId = session.deviceId, cloudReviewUid = review)

    @Test fun requestIntentIsTrimmedIsolatedInJobAndChangesCacheIdentity() {
        val account = account(); val repo = ContactRepository(context, account)
        val first = CloudAnalysisRequest.key("conv", 3, "analysis", null, null, null, " 我现在不困，还想聊 ")
        val second = CloudAnalysisRequest.key("conv", 3, "analysis", null, null, null, " 我想休息 ")
        assertNotEquals(first, second)
        assertEquals("conv|3|analysis|||", CloudAnalysisRequest.key("conv", 3, "analysis", null, null, null, "  "))
        val body = CloudAnalysisRequest.body("conv", 3, "analysis", null, null, JSONObject(), " 我现在不困，还想聊 ")
        assertEquals("我现在不困，还想聊", body.getString("current_intent"))
        repo.db.saveJob(first, body)
        assertEquals("我现在不困，还想聊", repo.db.job(first)!!.getJSONObject("body").getString("current_intent"))
        assertNull(repo.db.job(second))
        for (type in listOf("profiles", "notes", "preferences", "memories")) assertTrue(repo.db.records(type).isEmpty())
        assertEquals(0, repo.db.pendingCount())
        val followup = CloudAnalysisRequest.body("conv", 3, "rewrite", "parent", "句子", JSONObject(), "")
        assertFalse(followup.has("current_intent"))
        assertFalse(CloudAnalysisRequest.body("conv", 3, "analysis", null, null, JSONObject(), " ").has("current_intent"))
        assertThrows(IllegalArgumentException::class.java) {
            CloudAnalysisRequest.body("conv", 3, "analysis", null, null, JSONObject(), "字".repeat(2001))
        }
        assertEquals("🙂".repeat(2000), CloudAnalysisRequest.body("conv", 3, "analysis", null, null, JSONObject(), "🙂".repeat(2000)).getString("current_intent"))
        assertThrows(IllegalArgumentException::class.java) {
            CloudAnalysisRequest.body("conv", 3, "analysis", null, null, JSONObject(), " " + "字".repeat(2000))
        }
    }
}
