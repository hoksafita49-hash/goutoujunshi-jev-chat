package com.jev.probe.capture

import com.jev.probe.core.ChatSnapshot
import com.jev.probe.core.Msg
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.assertNotEquals
import org.junit.Test

class ChatCaptureServiceTest {
    @Test fun accessibilityEventsCannotReplacePendingReview() {
        assertTrue(shouldIgnoreAccessibilityEvents(reviewPending = true))
    }

    @Test fun accessibilityEventsResumeAfterReview() {
        assertFalse(shouldIgnoreAccessibilityEvents(reviewPending = false))
    }

    @Test fun confirmationWaitsForOverlayFocusToReturnToChat() {
        assertEquals(ReviewConfirmationState.WAIT_FOR_CHAT_WINDOW,
            reviewConfirmationState("com.goutoujunshi.chat",
                "com.goutoujunshi.chat", "cn.soulapp.android"))
        assertEquals(ReviewConfirmationState.WAIT_FOR_CHAT_WINDOW,
            reviewConfirmationState(null,
                "com.goutoujunshi.chat", "cn.soulapp.android"))
    }

    @Test fun confirmationAcceptsOnlyTheOriginalChatApp() {
        assertEquals(ReviewConfirmationState.CHAT_CURRENT,
            reviewConfirmationState("cn.soulapp.android",
                "com.goutoujunshi.chat", "cn.soulapp.android"))
        assertEquals(ReviewConfirmationState.CHAT_CHANGED,
            reviewConfirmationState("com.tencent.mobileqq",
                "com.goutoujunshi.chat", "cn.soulapp.android"))
    }

    @Test fun anUntitledCloudReviewRequiresTheOriginalVisibleTranscriptAfterFocusReturns() {
        val source = ChatSnapshot(null, listOf(Msg("other", "原始可见原文")))
        assertTrue(unreliableReviewMatchesLive(source, source))
        assertFalse(unreliableReviewMatchesLive(source.copy(messages = listOf(Msg("other", "另一个人的原文"))), source))
        assertFalse(unreliableReviewMatchesLive(source.copy(title = "新标题"), source))
        val placeholder = source.copy(title = "已确认对话")
        assertTrue(unreliableReviewMatchesLive(placeholder, placeholder))
    }

    @Test fun untitledReviewRejectsAnEarlierVisibleMessageChangingEvenWhenTheLastSixMatch() {
        val source = ChatSnapshot(null, (1..7).map { Msg("other", "原文 $it") })
        val changed = source.copy(messages = listOf(Msg("other", "另一个人的开头")) + source.messages.drop(1))
        assertFalse(unreliableReviewMatchesLive(changed, source))
    }

    @Test fun earlierUntitledSourceChangeRefreshesCaptureBeforeOpeningAnotherReview() {
        val source = ChatSnapshot(null, (1..7).map { Msg("other", "原文 $it") })
        val changed = source.copy(messages = listOf(Msg("other", "另一个人的开头")) + source.messages.drop(1))
        assertNotEquals(captureReviewSignature(source, true), captureReviewSignature(changed, true))
        assertEquals(source.signature(), captureReviewSignature(source, false))
        assertEquals(source.copy(title = "小王").signature(), captureReviewSignature(source.copy(title = "小王"), true))
    }
}
