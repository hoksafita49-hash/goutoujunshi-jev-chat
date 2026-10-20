package com.jev.probe.cloud

import com.jev.probe.core.Analysis
import com.jev.probe.core.RankedReply
import org.json.JSONObject
import java.net.URI
import java.time.Instant
import java.util.UUID

data class CloudSession(val baseUrl: String, val token: String, val userId: String,
    val email: String, val deviceId: String, val deviceSecret: String, val epoch: String) {
    val accountKey: String get() = "$baseUrl|$userId"
    fun json(): JSONObject = JSONObject().put("base_url", baseUrl).put("token", token)
        .put("user_id", userId).put("email", email).put("device_id", deviceId)
        .put("device_secret", deviceSecret).put("server_epoch", epoch)
    companion object {
        fun read(o: JSONObject) = CloudSession(CloudProtocol.baseUrl(o.getString("base_url")),
            o.getString("token"), o.getString("user_id"), o.getString("email"),
            o.getString("device_id"), o.getString("device_secret"), o.getString("server_epoch"))
    }
}

/** A rewrite is a separate text variant; it must never replace the parent candidate list. */
data class CloudRewrite(val parent: Analysis, val selectedText: String, val text: String, val taskId: String) {
    companion object {
        fun read(result: JSONObject, parent: Analysis, selected: String): CloudRewrite {
            require(result.optInt("schema_version") == 1) { "改写结果版本不兼容" }
            require(parent.rankedReplies.any { it.text == selected }) { "改写原文不属于当前候选" }
            require(result.getString("conversation_uid") == parent.conversationUid &&
                result.getInt("context_revision") == parent.contextRevision) { "改写结果不属于当前上下文" }
            val text = result.getString("text").trim()
            require(text.isNotBlank()) { "改写结果为空；原候选已保留" }
            return CloudRewrite(parent, selected, text, result.getString("analysis_id"))
        }
    }
}

object CloudProtocol {
    const val PREFERENCES_UID = "00000000-0000-4000-8000-000000000001"
    fun baseUrl(raw: String): String {
        val uri = try { URI(raw.trim()) } catch (_: Exception) { throw IllegalArgumentException("服务器地址无效") }
        require(uri.host != null && uri.userInfo == null && uri.rawQuery == null && uri.rawFragment == null &&
            (uri.path.isNullOrEmpty() || uri.path == "/")) { "服务器地址只能包含协议、主机和端口" }
        require(uri.scheme == "https" || (uri.scheme == "http" &&
            uri.host in listOf("127.0.0.1", "localhost") && uri.port == 18000)) {
            "请使用 HTTPS；开发仅允许 http://127.0.0.1:18000 或 localhost:18000"
        }
        return raw.trim().trimEnd('/')
    }
    fun legacyUid(account: String, source: String): String =
        UUID.nameUUIDFromBytes("android-cloud-v1|$account|$source".toByteArray(Charsets.UTF_8)).toString()
    fun now(): String = Instant.now().toString()
    fun strings(o: JSONObject, key: String): List<String> = o.optJSONArray(key)?.let { a ->
        (0 until a.length()).map { a.getString(it) }
    } ?: emptyList()
    fun analysis(o: JSONObject): Analysis {
        require(o.optInt("schema_version") == 1) { "不兼容的分析结果版本" }
        for (key in listOf("analysis_id", "conversation_uid", "context_revision", "candidates", "ranking_status"))
            require(o.has(key) && !o.isNull(key)) { "分析结果缺少 $key" }
        require(o.getString("ranking_status") in listOf("available", "unavailable")) { "排序状态无效" }
        val available = o.getString("ranking_status") == "available"
        val candidates = o.getJSONArray("candidates")
        val replies = (0 until candidates.length()).map { i ->
            val c = candidates.getJSONObject(i)
            require(c.has("candidate_id") && c.has("text")) { "候选回复不完整" }
            val weight = if (!available || c.isNull("weight")) null else c.getDouble("weight").also {
                require(it.isFinite() && it in 0.0..1.0) { "排序权重无效" }
            }
            RankedReply(c.getString("text"), weight, c.getString("candidate_id"))
        }
        return Analysis(null, null, null, null, null, null, null, replies, 0,
            strategy = o.optString("strategy"), strategyMethod = o.getString("ranking_status"),
            facts = strings(o, "facts"), unknowns = strings(o, "unknowns"),
            analysisId = o.getString("analysis_id"), conversationUid = o.getString("conversation_uid"),
            contextRevision = o.getInt("context_revision"), stopCondition = o.optString("stop_condition"),
            hypotheses = strings(o, "hypotheses"))
    }
    /** Sequence overlap only; two equal short messages at different positions remain distinct. */
    fun newScreenTail(previous: List<String>, screen: List<String>): List<String> {
        if (previous == screen) return emptyList()
        for (n in minOf(previous.size, screen.size) downTo 1)
            if (previous.takeLast(n) == screen.take(n)) return screen.drop(n)
        return screen
    }
}
