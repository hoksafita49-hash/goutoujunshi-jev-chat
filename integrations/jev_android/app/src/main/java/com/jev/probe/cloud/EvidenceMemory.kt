package com.jev.probe.cloud

import com.jev.probe.data.LocalDatabase
import org.json.JSONArray
import org.json.JSONObject
import java.net.URLEncoder
import java.util.UUID

object EvidenceMemoryPreferences {
    fun save(previous: JSONObject, tone: String, length: String, count: Int, style: String): JSONObject =
        JSONObject(previous.toString()).put("tone", tone).put("length", length).put("count", count.coerceIn(1, 3)).put("style", style)
    fun request(previous: JSONObject): JSONObject = save(previous, previous.optString("tone", "自然"),
        previous.optString("length", "适中"), previous.optInt("count", 3), previous.optString("style", ""))
}
data class EvidenceQuote(val messageUid: String, val quote: String, val speaker: String, val observedAt: String,
    val originalText: String = "", val version: Int = 0)
data class EvidenceClaim(val id: String, val kind: String, val text: String, val quotes: List<EvidenceQuote>)
data class EvidenceSnapshot(val profileUid: String, val claims: List<EvidenceClaim>) {
    companion object {
        fun read(raw: JSONObject, legacy: JSONObject): EvidenceSnapshot {
            val messages = raw.optJSONArray("messages") ?: JSONArray()
            val byUid = (0 until messages.length()).map { messages.getJSONObject(it) }.associateBy { it.getString("uid") }
            val evidence = raw.optJSONArray("evidence") ?: JSONArray()
            val claims = (0 until evidence.length()).map { index ->
                val claim = evidence.getJSONObject(index)
                val refs = CloudProtocol.strings(claim, "source_message_uids").toSet()
                val quotes = claim.optJSONArray("source_quotes") ?: JSONArray()
                val valid = if (claim.optString("citation_status") != "verified") emptyList() else (0 until quotes.length()).mapNotNull { n ->
                    val q = quotes.getJSONObject(n); val uid = q.optString("message_uid"); val text = q.optString("quote")
                    val message = byUid[uid]
                    if (uid !in refs || text.isBlank() || message == null || !message.optString("text").contains(text)) null
                    else EvidenceQuote(uid, text, message.optString("role", "unknown"), message.optString("observed_at"),
                        message.getString("text"), message.optInt("version"))
                }.distinct()
                EvidenceClaim(claim.optString("claim_id", "claim-$index"), claim.optString("kind", "unknown"), claim.optString("text"), valid)
            }.ifEmpty {
                listOf("facts" to "fact", "hypotheses" to "hypothesis", "unknowns" to "unknown").flatMap { (field, kind) ->
                    CloudProtocol.strings(legacy, field).mapIndexed { n, text -> EvidenceClaim("legacy-$field-$n", kind, text, emptyList()) }
                }
            }
            return EvidenceSnapshot(raw.getString("profile_uid"), claims)
        }
    }
}
data class MemoryHistory(val versions: List<JSONObject>, val hasMore: Boolean, val nextBeforeVersion: Int)
class EvidenceMemoryRepository(val db: LocalDatabase, val binding: CloudAccountBinding,
    private val read: (CloudSession, String) -> JSONObject = { session, path -> GatewayClient(session.baseUrl, session).request("GET", path) }) {
    init { require(db.account == binding.session.accountKey) { "资料不属于当前账号" } }
    fun create(profile: String, text: String, kind: String, reason: String = "手动录入"): JSONObject = create(profile, text, kind, emptyList(), reason)
    private fun create(profile: String, text: String, kind: String, sources: List<String>, reason: String): JSONObject = binding.withCurrent {
        db.transaction { sql ->
            check(db.record("profiles", profile)?.isNull("deleted_at") == true) { "人物不存在或已删除，请先同步" }
            val payload = JSONObject().put("profile_uid", profile).put("text", text.trim()).put("kind", kind)
                .put("status", "pending").put("source_uids", JSONArray(sources.distinct()))
                .put("source_type", if (sources.isEmpty()) "user" else "messages").put("change_reason", reason)
            validate(payload)
            val uid = UUID.randomUUID().toString()
            db.edit(sql, "memories", uid, payload)
            checkNotNull(db.record("memories", uid))
        }
    }
    fun fromClaim(profile: String, claim: EvidenceClaim): JSONObject = binding.withCurrent {
        val sources = claim.quotes.map { it.messageUid }.distinct()
        for (uid in sources) {
            val message = db.record("messages", uid)
            require(message?.isNull("deleted_at") == true && message.getJSONObject("payload").let {
                it.optString("profile_uid") == profile && it.optBoolean("confirmed")
            }) { "引用消息已删除、未确认或不属于此人物，请先同步" }
        }
        create(profile, claim.text, if (claim.kind == "fact") "fact" else "hypothesis", sources,
            if (sources.isEmpty()) "从分析说法录入；原话引用不可用，待人工核对" else "从分析原话保存，待人工核对")
    }
    fun update(current: JSONObject, text: String, kind: String, status: String, reason: String): JSONObject {
        require(kind != "hypothesis" || status != "confirmed") { "假设不能确认；请先明确转为事实并保存" }
        return mutate(current) { payload ->
            val changed = payload.optString("text") != text.trim() || payload.optString("kind") != kind
            if (!changed && status == "confirmed") check(!db.hasLocalChanges("memories", current.getString("uid"))) {
                "请先同步新增或更正，再单独确认事实，避免确认选择被合并到待确认修改中"
            }
            payload.put("text", text.trim()).put("kind", kind).put("status", if (changed) "pending" else status).put("change_reason", reason.trim())
        }
    }
    fun restore(current: JSONObject, selected: JSONObject): JSONObject {
        require(selected.optString("uid") == current.getString("uid") && selected.optString("entity_type") == "memories" && selected.isNull("deleted_at")) { "历史版本不属于此记忆" }
        require(selected.getJSONObject("payload").getString("profile_uid") == current.getJSONObject("payload").getString("profile_uid")) { "历史版本人物不匹配" }
        return mutate(current) { payload ->
            val old = selected.getJSONObject("payload")
            for (key in old.keys()) payload.put(key, old.get(key))
            payload.put("status", "pending").put("change_reason", "恢复版本 ${selected.getInt("version")}")
        }
    }
    fun delete(current: JSONObject, confirmed: Boolean) = binding.withCurrent {
        require(confirmed) { "删除需要明确确认" }
        db.transaction { sql ->
            assertVisible(current)
            db.edit(sql, "memories", current.getString("uid"), JSONObject(current.getJSONObject("payload").toString()), true)
        }
    }
    private fun mutate(current: JSONObject, edit: (JSONObject) -> Unit): JSONObject = binding.withCurrent {
        db.transaction { sql ->
            assertVisible(current)
            val payload = JSONObject(current.getJSONObject("payload").toString())
            edit(payload); validate(payload)
            db.edit(sql, "memories", current.getString("uid"), payload)
            checkNotNull(db.record("memories", current.getString("uid")))
        }
    }
    private fun assertVisible(expected: JSONObject) {
        val visible = db.record("memories", expected.getString("uid"))
        check(visible?.isNull("deleted_at") == true && visible.toString() == expected.toString()) { "记忆已有更新或已删除，请返回列表重新打开；原操作未覆盖最新版本" }
    }
    private fun validate(payload: JSONObject) {
        require(payload.getString("text").isNotBlank() && payload.getString("text").length <= 4000) { "记忆正文需为 1–4000 字" }
        require(payload.optString("kind") in listOf("fact", "hypothesis")) { "请选择事实或假设" }
        require(payload.optString("status") in listOf("pending", "confirmed")) { "确认状态无效" }
        require(payload.optString("kind") != "hypothesis" || payload.optString("status") != "confirmed") { "假设不能确认；请先明确转为事实并保存" }
        require(payload.optString("change_reason").length <= 500) { "修改原因不能超过 500 字" }
        val sources = CloudProtocol.strings(payload, "source_uids").distinct()
        require(sources.size <= 16) { "最多引用 16 条消息" }
        payload.put("source_uids", JSONArray(sources)).put("source_type", if (sources.isEmpty()) "user" else "messages")
        for (uid in sources) db.record("messages", uid)?.let { record ->
            require(record.isNull("deleted_at") && record.getJSONObject("payload").let {
                it.optString("profile_uid") == payload.getString("profile_uid") && it.optBoolean("confirmed")
            }) { "来源消息已删除、未确认或人物不匹配" }
        }
    }
    fun evidence(analysis: JSONObject): EvidenceSnapshot {
        val uid = analysis.getString("uid"); val legacy = analysis.getJSONObject("payload")
        val result = legacy.optJSONObject("result") ?: legacy
        val conversation = legacy.optString("conversation_uid", result.optString("conversation_uid"))
        val revision = legacy.optInt("context_revision", result.optInt("context_revision"))
        val expectedProfile = binding.withCurrent {
            legacy.optString("profile_uid").takeIf { it.isNotBlank() } ?: db.record("conversations", conversation)
                ?.optJSONObject("payload")?.optString("profile_uid") ?: error("此旧分析缺少人物关联，请先同步")
        }
        val response = remote("/v1/analyses/${pathUid(uid)}/evidence")
        return binding.withCurrent {
            require(response.getString("analysis_id") == uid && response.getString("conversation_uid") == conversation &&
                response.getString("profile_uid") == expectedProfile && response.getInt("context_revision") == revision) { "证据不属于此分析上下文" }
            check(db.record("analyses", uid)?.isNull("deleted_at") == true) { "分析已删除，请返回列表" }
            EvidenceSnapshot.read(response, result)
        }
    }
    fun history(uid: String, beforeVersion: Int = 0): MemoryHistory {
        require(beforeVersion >= 0)
        val expectedProfile = binding.withCurrent { checkNotNull(db.record("memories", uid)).also { check(it.isNull("deleted_at")) }.getJSONObject("payload").getString("profile_uid") }
        val response = remote("/v1/memories/${pathUid(uid)}/history?before_version=$beforeVersion&limit=50")
        return binding.withCurrent {
            check(db.record("memories", uid)?.isNull("deleted_at") == true) { "记忆已删除，请返回列表" }
            require(response.getString("memory_uid") == uid && response.getString("profile_uid") == expectedProfile) { "历史不属于此记忆或人物" }
            val array = response.getJSONArray("versions")
            val versions = (0 until array.length()).map { array.getJSONObject(it) }
            require(versions.size <= 50 && versions.all { it.optString("entity_type") == "memories" && it.getString("uid") == uid &&
                it.getJSONObject("payload").getString("profile_uid") == expectedProfile && it.isNull("deleted_at") }) { "历史版本格式无效" }
            val more = response.optBoolean("has_more"); val next = response.optInt("next_before_version")
            require(!more || (next > 0 && (beforeVersion == 0 || next < beforeVersion))) { "历史分页游标无效" }
            MemoryHistory(versions, more, next)
        }
    }
    private fun remote(path: String): JSONObject {
        val response = read(binding.withCurrent { it }, path)
        binding.assertCurrent()
        return response
    }
    private fun pathUid(uid: String) = URLEncoder.encode(uid, "UTF-8")
}
