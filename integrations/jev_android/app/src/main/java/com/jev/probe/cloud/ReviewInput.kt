package com.jev.probe.cloud

import com.jev.probe.core.kb.Contact
import org.json.JSONObject
import java.util.UUID
import java.util.Locale

/** A choice made in the current review, never inferred from equal person names. */
data class ReviewPersonChoice(val profileUid: String? = null, val newName: String? = null)
data class CloudReviewChoice(val person: ReviewPersonChoice, val currentIntent: String = "")
data class CloudReviewOptions(val contacts: List<Contact>, val preselectedProfileUid: String? = null)

object ReviewTitle {
    private val placeholders = setOf("已确认对话", "当前会话", "当前对话", "未知会话", "未命名对话", "聊天记录", "未知", "未识别",
        "未识别会话", "未识别到会话", "尚未识别到会话", "未命名会话", "未命名", "微信", "wechat", "qq", "unknown", "untitled", "抖音", "douyin")
    private val transient = listOf("连接中", "正在连接", "未连接", "connecting", "加载中", "loading", "同步中", "syncing")
    fun reliable(title: String?): Boolean {
        val text = title?.trim()?.removeSuffix("…")?.removeSuffix("...")?.trim().orEmpty()
        return text.isNotEmpty() && text.lowercase(Locale.ROOT) !in placeholders && transient.none { text.contains(it, ignoreCase = true) }
    }
    fun display(title: String?): String = title?.trim()?.takeIf { it.isNotEmpty() } ?: "已确认对话"
}

/** One request's private supplement. It belongs to the durable job, never a person or preference. */
object CloudAnalysisRequest {
    fun intent(raw: String): String {
        require(raw.codePointCount(0, raw.length) <= 2000) { "本次补充最多 2000 字，请缩短后再分析" }
        return raw.trim()
    }
    fun key(conversation: String, revision: Int, kind: String, parent: String?, selected: String?,
        storedPreferences: JSONObject?, currentIntent: String = ""): String {
        val text = intent(currentIntent)
        require(kind == "analysis" || text.isEmpty()) { "后续任务沿用原分析的本次补充" }
        val legacy = "$conversation|$revision|$kind|${parent.orEmpty()}|${selected.orEmpty()}|${storedPreferences?.toString().orEmpty()}"
        return if (text.isEmpty()) legacy else "$legacy|current_intent:${text.length}:$text"
    }
    fun body(conversation: String, revision: Int, kind: String, parent: String?, selected: String?,
        preferences: JSONObject, currentIntent: String = ""): JSONObject {
        val text = intent(currentIntent)
        require(kind == "analysis" || text.isEmpty()) { "后续任务沿用原分析的本次补充" }
        return JSONObject().put("request_id", UUID.randomUUID().toString()).put("conversation_uid", conversation)
            .put("context_revision", revision).put("kind", kind).put("preferences", preferences).apply {
                parent?.let { put("parent_analysis_id", it) }; selected?.let { put("selected_text", it) }
                if (text.isNotEmpty()) put("current_intent", text)
            }
    }
}
