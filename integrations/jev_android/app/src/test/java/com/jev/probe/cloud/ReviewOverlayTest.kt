package com.jev.probe.cloud

import android.content.Context
import android.view.View
import android.view.ViewGroup
import android.widget.EditText
import android.widget.Spinner
import android.widget.TextView
import androidx.test.core.app.ApplicationProvider
import com.jev.probe.core.ChatSnapshot
import com.jev.probe.core.Msg
import com.jev.probe.core.kb.Contact
import com.jev.probe.data.ContactRepository
import com.jev.probe.overlay.OverlayController
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowSettings
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [30], application = android.app.Application::class)
class ReviewOverlayTest {
    private val context: Context get() = ApplicationProvider.getApplicationContext()
    private val snapshot = ChatSnapshot("可靠标题", listOf(Msg("other", "你好")))
    private val options = CloudReviewOptions(listOf(Contact("a", "甲"), Contact("b", "乙")), "a")
    private fun views(overlay: OverlayController): List<View> {
        val root = OverlayController::class.java.getDeclaredField("root").apply { isAccessible = true }.get(overlay) as View
        fun descendants(view: View): List<View> = listOf(view) + if (view is ViewGroup)
            (0 until view.childCount).flatMap { descendants(view.getChildAt(it)) } else emptyList()
        return descendants(root)
    }
    private fun confirm(overlay: OverlayController) = views(overlay).filterIsInstance<TextView>()
        .single { it.text.toString() == "确认原文并分析" }.performClick()

    @Test fun boundPersonCanBeChangedAndSupplementResetsOnTheNextReview() {
        ShadowSettings.setCanDrawOverlays(true)
        val overlay = OverlayController(context)
        var choice: CloudReviewChoice? = null
        overlay.showReview(snapshot, { _, selected -> choice = selected }, {}, options)
        val spinner = views(overlay).filterIsInstance<Spinner>().single()
        assertEquals(1, spinner.selectedItemPosition)
        spinner.setSelection(2)
        val supplement = views(overlay).filterIsInstance<EditText>().single { it.contentDescription == "本次补充" }
        supplement.setText(" 我现在不困，还想聊 ")
        confirm(overlay)
        assertEquals("b", choice!!.person.profileUid)
        assertEquals("我现在不困，还想聊", choice!!.currentIntent)
        overlay.showReview(snapshot, { _, _ -> }, {}, options)
        assertEquals("", views(overlay).filterIsInstance<EditText>().single { it.contentDescription == "本次补充" }.text.toString())
        overlay.hide()
    }

    @Test fun placeholderRequiresAnActiveChoiceAndNewNameBeforeConfirming() {
        ShadowSettings.setCanDrawOverlays(true)
        val overlay = OverlayController(context)
        var choice: CloudReviewChoice? = null
        overlay.showReview(snapshot.copy(title = "已确认对话"), { _, selected -> choice = selected }, {}, options)
        val spinner = views(overlay).filterIsInstance<Spinner>().single()
        assertEquals(0, spinner.selectedItemPosition)
        confirm(overlay); assertNull(choice)
        spinner.setSelection(spinner.count - 1)
        confirm(overlay); assertNull(choice)
        views(overlay).filterIsInstance<EditText>().single { it.contentDescription == "新人物称呼" }.setText("新朋友")
        confirm(overlay)
        assertEquals("新朋友", choice!!.person.newName)
        overlay.hide()
    }

    @Test fun supplementLimitCountsUnicodeCharactersWithoutCuttingEmoji() {
        ShadowSettings.setCanDrawOverlays(true)
        val overlay = OverlayController(context)
        var choice: CloudReviewChoice? = null
        overlay.showReview(snapshot, { _, selected -> choice = selected }, {}, options)
        val supplement = views(overlay).filterIsInstance<EditText>().single { it.contentDescription == "本次补充" }
        supplement.setText("🙂".repeat(2001))
        confirm(overlay); assertNull(choice)
        assertEquals(4002, supplement.text.length)
        supplement.setText("🙂".repeat(2000))
        confirm(overlay)
        assertEquals("🙂".repeat(2000), choice!!.currentIntent)
        overlay.hide()
    }

    @Test fun directModeDoesNotShowAnIgnoredSupplement() {
        ShadowSettings.setCanDrawOverlays(true)
        val overlay = OverlayController(context)
        var confirmed = false
        overlay.showReview(snapshot, { _, selected -> confirmed = true; assertNull(selected) }, {})
        assertTrue(views(overlay).filterIsInstance<Spinner>().isEmpty())
        assertFalse(views(overlay).any { it.contentDescription == "本次补充" })
        confirm(overlay); assertTrue(confirmed)
        overlay.hide()
    }

    @Test fun openingChangingNewPersonInputsAndCancellingDoesNotWriteCloudRecordsOrOutbox() {
        ShadowSettings.setCanDrawOverlays(true)
        val account = CloudSession("https://example.com", "token", UUID.randomUUID().toString(), "test", "device", "secret", "epoch")
        val repo = ContactRepository(context, account)
        repo.saveContact(Contact("a", "甲"))
        val before = repo.db.pendingCount()
        val overlay = OverlayController(context)
        var cancelled = false
        overlay.showReview(snapshot, { _, _ -> fail("cancel must not confirm") }, { cancelled = true }, CloudReviewOptions(repo.contacts()))
        val spinner = views(overlay).filterIsInstance<Spinner>().single()
        spinner.setSelection(spinner.count - 1)
        views(overlay).filterIsInstance<EditText>().single { it.contentDescription == "新人物称呼" }.setText("尚未创建")
        views(overlay).filterIsInstance<EditText>().single { it.contentDescription == "本次补充" }.setText("尚未提交的想法")
        views(overlay).filterIsInstance<TextView>().single { it.text.toString() == "✕" }.performClick()
        assertTrue(cancelled)
        assertEquals(before, repo.db.pendingCount())
        assertEquals(listOf("a"), repo.contacts().map { it.id })
        for (type in listOf("conversations", "identity_links", "messages", "notes", "preferences", "memories"))
            assertTrue(repo.db.records(type).isEmpty())
        assertTrue(repo.db.jobs().isEmpty())
        overlay.hide()
    }
}
