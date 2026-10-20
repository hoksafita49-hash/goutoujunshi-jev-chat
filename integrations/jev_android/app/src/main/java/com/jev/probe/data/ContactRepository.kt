package com.jev.probe.data

import android.content.Context
import com.jev.probe.cloud.CloudProtocol
import com.jev.probe.cloud.CloudSession
import com.jev.probe.cloud.CloudAccountBinding
import com.jev.probe.core.ChatSnapshot
import com.jev.probe.core.Prefs
import com.jev.probe.core.kb.*
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.util.UUID

class ContactRepository(context: Context, val session: CloudSession) {
    val db = LocalDatabase.get(context, session.accountKey)
    private val sourceDevice = Prefs(context).installationId
    private fun payload(record: JSONObject) = record.getJSONObject("payload")
    private fun time(record: JSONObject) = runCatching { Instant.parse(record.getString("updated_at")).toEpochMilli() }.getOrDefault(0L)
    fun contacts(): List<Contact> {
        val links = db.records("identity_links")
        return db.records("profiles").map { record ->
            val p = payload(record); val uid = record.getString("uid")
            val identities = links.filter { payload(it).optString("profile_uid") == uid &&
                payload(it).optString("source_device") == sourceDevice }.map { payload(it) }
            Contact(uid, p.optString("name"), identities.map { it.optString("legacy_id") }.filter { it.isNotBlank() },
                identities.map { it.optString("platform") }.filter { it.isNotBlank() }.distinct(),
                p.optString("background"), p.optString("notes"), p.optString("stage", "未填写"), p.optString("goal", "自然接话"),
                autoSummary = p.optString("auto_summary"), updatedAt = time(record))
        }
    }
    fun contact(id: String): Contact? = contacts().firstOrNull { it.id == id }
    fun findContact(title: String, app: String): Contact? {
        // Only explicitly assigned identities match. Downloaded equal names never silently merge.
        val links = db.records("identity_links").map { payload(it) }
        val uid = links.lastOrNull { it.optString("platform") == app && it.optString("legacy_id") == title &&
            it.optString("source_device") == sourceDevice }?.optString("profile_uid")
        return uid?.let { contact(it) }
    }
    fun saveContact(c: Contact): Boolean {
        db.transaction { sql ->
            db.edit(sql, "profiles", c.id, JSONObject().put("name", c.name).put("stage", c.stage)
                .put("goal", c.goal).put("background", c.relationship).put("notes", c.notes).put("auto_summary", c.autoSummary))
            for (app in c.apps) for (title in (c.aliases + c.name).distinct()) {
                replaceIdentity(sql, c.id, title, app)
            }
        }
        return true
    }
    fun bindContact(id: String, title: String, app: String) {
        checkNotNull(contact(id)) { "联系人不存在" }
        db.transaction { sql -> replaceIdentity(sql, id, title, app) }
    }
    private fun replaceIdentity(sql: android.database.sqlite.SQLiteDatabase, person: String, title: String, app: String) {
        val previous = db.records("identity_links").filter { r -> payload(r).let {
            it.optString("source_device") == sourceDevice && it.optString("platform") == app && it.optString("legacy_id") == title
        } }
        val mapped = db.legacyUid("identity/$sourceDevice/$app/$title", sql)
        val uid = previous.lastOrNull()?.getString("uid") ?: if (db.record("identity_links", mapped)?.isNull("deleted_at") == false)
            UUID.randomUUID().toString() else mapped
        for (old in previous) if (old.getString("uid") != uid) db.edit(sql, "identity_links", old.getString("uid"), payload(old), true)
        db.edit(sql, "identity_links", uid, JSONObject().put("profile_uid", person).put("platform", app)
            .put("legacy_id", title).put("source_device", sourceDevice))
    }
    fun saveOrMergeContact(title: String, app: String): String {
        if (findContact(title, app) != null) return "当前聊天已关联云联系人"
        saveContact(Contact(UUID.randomUUID().toString(), title, listOf(title), listOf(app)))
        return "已创建独立云联系人「$title」；同名人物不会自动合并"
    }
    fun deleteContact(id: String): Boolean {
        val p = db.record("profiles", id)?.getJSONObject("payload") ?: return true
        db.edit("profiles", id, p, true)
        return true
    }
    fun notes(): List<Note> = db.records("notes").map { r -> payload(r).let { p ->
        Note(r.getString("uid"), p.optString("title"), p.optString("body"), CloudProtocol.strings(p, "tags"),
            alwaysOn = p.optBoolean("always_on", false), enabled = p.optBoolean("enabled", true), updatedAt = time(r))
    } }
    fun saveNote(n: Note): Boolean {
        db.edit("notes", n.id, JSONObject().put("title", n.title).put("body", n.content).put("tags", JSONArray(n.tags))
            .put("enabled", n.enabled).put("always_on", n.alwaysOn))
        return true
    }
    fun deleteNote(id: String): Boolean {
        db.record("notes", id)?.getJSONObject("payload")?.let { db.edit("notes", id, it, true) }
        return true
    }
    fun logs(id: String): List<LogEntry> = db.records("messages").map { payload(it) }.filter { it.optString("profile_uid") == id }
        .map { LogEntry(it.optString("role", "unknown"), it.optString("text"),
            runCatching { Instant.parse(it.getString("observed_at")).toEpochMilli() }.getOrDefault(0L), "云端") }
    fun clearLog(id: String) {
        for (r in db.records("messages")) if (payload(r).optString("profile_uid") == id)
            db.edit("messages", r.getString("uid"), payload(r), true)
    }
    /** Called only after the existing raw-text review was confirmed by the user. */
    fun confirm(snapshot: ChatSnapshot, app: String, source: ChatSnapshot, binding: CloudAccountBinding,
        expectedProfileUid: String? = null): Pair<String, String> = binding.withCurrent { db.transaction {
        check(binding.session.accountKey == session.accountKey && binding.session.deviceId == session.deviceId) { "核对账号与资料库不匹配" }
        val title = snapshot.title?.takeIf { it.isNotBlank() } ?: "已确认对话"
        val person = findContact(title, app)
            ?: error("此聊天尚未关联云人物。请长按悬浮球选择保存联系人，在云账号页选已有云人物或明确新建，再返回核对分析。")
        check(expectedProfileUid == null || person.id == expectedProfileUid) { "核对期间人物关联已改变，请重新识别并核对" }
        val conversation = db.transaction { sql ->
            val uid = db.legacyUid("conversation/${person.id}/$app/$title", sql)
            if (db.record("conversations", uid) == null) db.edit(sql, "conversations", uid, JSONObject()
                .put("profile_uid", person.id).put("title", title).put("platform", app).put("context_revision", 0))
            uid
        }
        db.transaction { sql ->
            val metaKey = "screen/$conversation"
            val prev = db.meta(metaKey)?.let { raw -> JSONArray(raw).let { a -> (0 until a.length()).map { a.getString(it) } } } ?: emptyList()
            val screen = snapshot.messages.map { "${it.side}\u0000${it.text}" }
            val sourceKeys = source.messages.map { "${it.side}\u0000${it.text}" }
            val sourceJson = JSONArray(sourceKeys).toString()
            val previousRows = db.meta("screen_rows/$conversation")?.let { JSONArray(it) } ?: JSONArray()
            val sameReviewedSource = db.meta("screen_source/$conversation") == sourceJson && previousRows.length() == prev.size
            val tail = if (sameReviewedSource) emptyList() else CloudProtocol.newScreenTail(prev, screen)
            val retained = if (sameReviewedSource) 0 else screen.size - tail.size
            val rows = JSONArray()
            val orderPrefix = "%020d".format(System.currentTimeMillis()) + "/" + session.deviceId + "/"
            for ((index, key) in screen.withIndex()) {
                val side = key.substringBefore('\u0000'); val text = key.substringAfter('\u0000')
                if (text.isBlank()) continue
                if (!sameReviewedSource && index < retained && previousRows.length() >= retained) {
                    rows.put(previousRows.getString(previousRows.length() - retained + index))
                    continue
                }
                val existingUid = if (sameReviewedSource && index < previousRows.length()) previousRows.getString(index) else null
                val old = existingUid?.let { db.record("messages", it)?.optJSONObject("payload") }
                val uid = existingUid ?: UUID.randomUUID().toString()
                val message = JSONObject().put("conversation_uid", conversation)
                    .put("profile_uid", person.id).put("role", if (side in listOf("me", "other")) side else "unknown")
                    .put("text", text).put("confirmed", true).put("observed_at", old?.optString("observed_at") ?: CloudProtocol.now())
                    .put("order_key", old?.optString("order_key") ?: orderPrefix + "%08d".format(index))
                if (old == null || old.optString("text") != text || old.optString("role") != side)
                    db.edit(sql, "messages", uid, message)
                rows.put(uid)
            }
            if (sameReviewedSource) for (index in screen.size until previousRows.length()) {
                val uid = previousRows.getString(index)
                db.record("messages", uid)?.optJSONObject("payload")?.let { db.edit(sql, "messages", uid, it, true) }
            }
            sql.execSQL("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", arrayOf(metaKey, JSONArray(screen).toString()))
            sql.execSQL("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", arrayOf("screen_source/$conversation", sourceJson))
            sql.execSQL("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", arrayOf("screen_rows/$conversation", rows.toString()))
        }
        person.id to conversation
    } }
}
