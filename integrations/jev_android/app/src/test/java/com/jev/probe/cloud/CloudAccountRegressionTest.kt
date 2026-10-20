package com.jev.probe.cloud

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import com.jev.probe.core.ChatSnapshot
import com.jev.probe.core.Msg
import com.jev.probe.core.kb.Contact
import com.jev.probe.data.ContactRepository
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.util.UUID
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import java.util.concurrent.CancellationException

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [30], application = android.app.Application::class)
class CloudAccountRegressionTest {
    private val context: Context get() = ApplicationProvider.getApplicationContext()
    private fun account() = CloudSession("https://example.com", "token", UUID.randomUUID().toString(), "test", UUID.randomUUID().toString(), "secret", "epoch")
    private fun person(repo: ContactRepository, name: String = "人物") = Contact(UUID.randomUUID().toString(), name, listOf("聊天"), listOf("chat")).also { repo.saveContact(it) }

    @Test fun queuedAnalysisFromAccountACannotConfirmOrEnqueueIntoAccountB() {
        val a = account(); val b = account()
        val repositoryA = ContactRepository(context, a); val repositoryB = ContactRepository(context, b)
        person(repositoryA); person(repositoryB)
        val beforeA = repositoryA.db.pendingCount(); val beforeB = repositoryB.db.pendingCount()
        val active = AtomicReference(a)
        val access = object : CloudSessionAccess { override fun current() = active.get() }
        val analysis = CloudAnalysis(context, access) // created when the review belongs to A
        val blocked = CountDownLatch(1); val release = CountDownLatch(1)
        val worker = Executors.newSingleThreadExecutor()
        try {
            worker.submit { blocked.countDown(); release.await() }
            assertTrue(blocked.await(5, TimeUnit.SECONDS))
            val pending = worker.submit { runCatching { analysis.analyze(ChatSnapshot("聊天", listOf(Msg("other", "A 的确认私密文本"))), "chat") } }
            active.set(b)
            release.countDown(); pending.get(10, TimeUnit.SECONDS)
            assertTrue(repositoryB.db.records("messages").isEmpty())
            assertTrue(repositoryA.db.records("messages").isEmpty())
            assertEquals(beforeB, repositoryB.db.pendingCount()); assertEquals(beforeA, repositoryA.db.pendingCount())
            assertTrue(repositoryB.db.jobs().isEmpty())
        } finally { release.countDown(); worker.shutdownNow() }
    }
    @Test fun queuedReviewRejectsBWithAnAnalyzerConstructedAfterSwitch() {
        val a = account(); val b = account()
        val repositoryA = ContactRepository(context, a); val repositoryB = ContactRepository(context, b)
        person(repositoryA); person(repositoryB)
        val active = AtomicReference(a)
        val access = object : CloudSessionAccess { override fun current() = active.get() }
        val raw = ChatSnapshot("聊天", listOf(Msg("other", "A核对的原文")))
        val review = CloudReview(CloudAccountBinding.capture(access), raw, "chat")
        val worker = Executors.newSingleThreadExecutor(); val blocked = CountDownLatch(1); val release = CountDownLatch(1)
        try {
            worker.submit { blocked.countDown(); release.await() }; assertTrue(blocked.await(5, TimeUnit.SECONDS))
            val pending = worker.submit { assertThrows(CancellationException::class.java) {
                CloudAnalysis(context, access, review.binding).analyze(raw, "chat", review.source)
            } }
            active.set(b); release.countDown(); pending.get(10, TimeUnit.SECONDS)
            assertTrue(repositoryA.db.records("messages").isEmpty()); assertTrue(repositoryB.db.records("messages").isEmpty())
        } finally { release.countDown(); worker.shutdownNow() }
    }
    @Test fun unchangedBoundAccountCanConfirmIntoItsOwnSqliteBeforeTheNetworkStage() {
        val session = account(); val repository = ContactRepository(context, session); person(repository)
        val access = object : CloudSessionAccess { override fun current() = session }
        val analysis = CloudAnalysis(context, access)
        // The real transport gate has no Android Keystore login in this test; the local stage must still be real.
        runCatching { analysis.analyze(ChatSnapshot("聊天", listOf(Msg("other", "本账号的确认原文"))), "chat") }
        assertEquals("本账号的确认原文", repository.db.records("messages").single().getJSONObject("payload").getString("text"))
    }
    @Test fun switchingAwayAndBackOrChangingDeviceInvalidatesAnOldReview() {
        val a = account(); val active = AtomicReference(a)
        val access = object : CloudSessionAccess { override fun current() = active.get() }
        val binding = CloudAccountBinding.capture(access)
        active.set(account()); CloudAccountBoundary.changed()
        active.set(a); CloudAccountBoundary.changed()
        assertThrows(CancellationException::class.java) { binding.assertCurrent() }
        val next = CloudAccountBinding.capture(access)
        active.set(a.copy(deviceId = UUID.randomUUID().toString()))
        assertThrows(CancellationException::class.java) { next.assertCurrent() }
    }
    @Test fun explicitRebindingReplacesThisDevicesOldLinkAndForeignLinksNeverAutoMatch() {
        val session = account(); val repository = ContactRepository(context, session)
        val a = person(repository, "甲")
        val b = Contact(UUID.randomUUID().toString(), "乙").also { repository.saveContact(it) }
        repository.bindContact(b.id, "聊天", "chat")
        assertEquals(b.id, repository.findContact("聊天", "chat")!!.id)
        repository.saveContact(repository.contact(a.id)!!.copy(notes = "仅修改甲的备注"))
        assertEquals(b.id, repository.findContact("聊天", "chat")!!.id)
        repository.db.edit("identity_links", UUID.randomUUID().toString(), JSONObject().put("profile_uid", a.id)
            .put("platform", "chat").put("legacy_id", "别台设备同名").put("source_device", "another-device"))
        assertNull(repository.findContact("别台设备同名", "chat"))
    }
    @Test fun correctedConfirmedTranscriptRevisesExistingMessageInsteadOfAppendingOldAndNew() {
        val session = account(); val repository = ContactRepository(context, session); person(repository)
        val access = object : CloudSessionAccess { override fun current() = session }
        val binding = CloudAccountBinding.capture(access)
        val raw = ChatSnapshot("聊天", listOf(Msg("other", "你好"), Msg("other", "行")))
        repository.confirm(raw, "chat", raw, binding)
        val operations = repository.db.batch()
        val receipts = org.json.JSONArray()
        for (i in 0 until operations.length()) {
            val op = operations.getJSONObject(i)
            receipts.put(JSONObject().put("op_id", op.getString("op_id")).put("status", "applied").put("record", JSONObject()
                .put("entity_type", op.getString("entity_type")).put("uid", op.getString("entity_uid")).put("version", 1)
                .put("created_at", "2026-10-21T00:00:00Z").put("updated_at", "2026-10-21T00:00:00Z")
                .put("deleted_at", JSONObject.NULL).put("payload", op.getJSONObject("payload"))))
        }
        repository.db.receipts(receipts)
        val original = repository.db.records("messages")
        repository.confirm(ChatSnapshot("聊天", listOf(Msg("other", "你好"), Msg("other", "不行"))), "chat", raw, binding)
        val corrected = repository.db.records("messages")
        assertEquals(2, corrected.size)
        assertEquals(original.map { it.getString("uid") }, corrected.map { it.getString("uid") })
        assertEquals(listOf("你好", "不行"), corrected.map { it.getJSONObject("payload").getString("text") })
        val correction = repository.db.batch()
        assertEquals(1, correction.length()); assertEquals(1, correction.getJSONObject(0).getInt("base_version"))
        assertEquals("messages", correction.getJSONObject(0).getString("entity_type"))
    }
    @Test fun shortMessageCorrectionKeepsThreeDistinctIds() {
        val session = account(); val repository = ContactRepository(context, session); person(repository)
        val binding = CloudAccountBinding.capture(object : CloudSessionAccess { override fun current() = session })
        val first = ChatSnapshot("聊天", listOf(Msg("other", "好"), Msg("other", "好")))
        repository.confirm(first, "chat", first, binding)
        val next = ChatSnapshot("聊天", first.messages + Msg("other", "好"))
        repository.confirm(next, "chat", next, binding)
        val ids = repository.db.records("messages").map { it.getString("uid") }
        assertEquals(3, ids.size)
        repository.confirm(next.copy(messages = listOf(Msg("other", "好"), Msg("other", "不好"), Msg("other", "好"))), "chat", next, binding)
        val corrected = repository.db.records("messages")
        assertEquals(ids, corrected.map { it.getString("uid") })
        assertEquals(listOf("好", "不好", "好"), corrected.map { it.getJSONObject("payload").getString("text") })
    }
    @Test fun rebindingWhileReviewWaitsCannotWriteToTheNewPerson() {
        val session = account(); val repository = ContactRepository(context, session)
        val a = person(repository, "甲")
        val b = Contact(UUID.randomUUID().toString(), "乙").also { repository.saveContact(it) }
        val binding = CloudAccountBinding.capture(object : CloudSessionAccess { override fun current() = session })
        val raw = ChatSnapshot("聊天", listOf(Msg("other", "原人物的核对文本")))
        val review = CloudReview(binding, raw, "chat", a.id)
        repository.bindContact(b.id, "聊天", "chat")
        assertThrows(IllegalStateException::class.java) {
            repository.confirm(raw, review.app, review.source, review.binding, review.profileUid)
        }
        assertTrue(repository.db.records("messages").isEmpty())
    }
}
