package com.jev.probe.sync

import android.content.Context
import com.jev.probe.cloud.CloudProtocol
import com.jev.probe.cloud.CloudSession
import com.jev.probe.data.LocalDatabase
import com.jev.probe.core.Prefs
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.time.Instant

class LegacyImporter(context: Context, private val session: CloudSession) {
    private val root = File(context.filesDir, "kb")
    private val db = LocalDatabase.get(context, session.accountKey)
    private val sourceDevice = Prefs(context).installationId
    private fun source(path: String) = "$sourceDevice/$path"
    private fun array(file: File): JSONArray = if (file.exists()) JSONArray(file.readText()) else JSONArray()
    private fun contacts() = array(File(root, "contacts.json"))
    fun preview(): String {
        if (db.meta("legacy_imported") == "true") return "此账号已经导入过本机旧 JSON，重复导入不会新增记录。原文件仍保留。"
        val contacts = contacts(); val notes = array(File(root, "notes.json"))
        return buildString {
            append("旧文件保持原样。联系人 ${contacts.length()}，通用笔记 ${notes.length()}。\n确认导入后，这些资料与下列历史将上传当前账号。请逐项核对原文；未知说话人仍为 unknown。\n\n")
            for (i in 0 until contacts.length()) {
                val c = contacts.getJSONObject(i)
                append("人物：${c.optString("name")}\n关系：${c.optString("relationship")}\n备注：${c.optString("notes")}\n")
                val logs = array(File(root, "logs/${c.getString("id")}.json"))
                for (j in 0 until logs.length()) { val l = logs.getJSONObject(j); append("${l.optString("side")}: ${l.optString("text")}\n") }
                append('\n')
            }
            for (i in 0 until notes.length()) { val n = notes.getJSONObject(i); append("笔记：${n.optString("title")}\n${n.optString("content")}\n\n") }
        }
    }
    fun importConfirmed(): String {
        if (db.meta("legacy_imported") == "true") return "已经导入，未重复新增"
        check(db.meta("recovery_required") != "true") { "请先处理恢复副本" }
        val contacts = contacts(); val notes = array(File(root, "notes.json"))
        var lines = 0
        db.transaction { sql ->
            for (i in 0 until contacts.length()) {
                val c = contacts.getJSONObject(i); val legacy = c.getString("id")
                val uid = db.legacyUid(source("kb/contacts/$legacy"), sql)
                db.edit(sql, "profiles", uid, JSONObject().put("name", c.optString("name"))
                    .put("stage", c.optString("stage", "未填写")).put("goal", c.optString("goal", "自然接话"))
                    .put("background", c.optString("relationship")).put("notes", c.optString("notes"))
                    .put("auto_summary", c.optString("autoSummary")))
                val apps = CloudProtocol.strings(c, "apps").ifEmpty { listOf("legacy_android") }
                val titles = (CloudProtocol.strings(c, "aliases") + c.optString("name")).distinct()
                for (app in apps) for (title in titles) db.edit(sql, "identity_links", db.legacyUid(source("kb/identity/$legacy/$app/$title"), sql),
                    JSONObject().put("profile_uid", uid).put("platform", app).put("legacy_id", title).put("source_device", sourceDevice))
                val logs = array(File(root, "logs/$legacy.json"))
                if (logs.length() > 0) {
                    val conv = db.legacyUid(source("kb/conversation/$legacy"), sql)
                    db.edit(sql, "conversations", conv, JSONObject().put("profile_uid", uid).put("title", c.optString("name"))
                        .put("platform", "legacy_android").put("context_revision", 0))
                    for (j in 0 until logs.length()) {
                        val l = logs.getJSONObject(j); val role = l.optString("side")
                        db.edit(sql, "messages", db.legacyUid(source("kb/logs/$legacy/$j"), sql), JSONObject()
                            .put("conversation_uid", conv).put("profile_uid", uid).put("role", if (role in listOf("me", "other")) role else "unknown")
                            .put("text", l.optString("text")).put("confirmed", true)
                            .put("observed_at", Instant.ofEpochMilli(l.optLong("ts", 0)).toString())
                            .put("order_key", "%020d".format(l.optLong("ts", 0)) + "/legacy/$legacy/" + "%08d".format(j)))
                        lines++
                    }
                }
            }
            for (i in 0 until notes.length()) {
                val n = notes.getJSONObject(i)
                db.edit(sql, "notes", db.legacyUid(source("kb/notes/${n.getString("id")}"), sql), JSONObject()
                    .put("title", n.optString("title")).put("body", n.optString("content")).put("tags", n.optJSONArray("tags") ?: JSONArray())
                    .put("enabled", n.optBoolean("enabled", true)).put("always_on", n.optBoolean("alwaysOn", false)))
            }
            sql.execSQL("INSERT OR REPLACE INTO meta(k,v) VALUES('legacy_imported','true')")
        }
        return "已导入 ${contacts.length()} 位联系人、${notes.length()} 篇通用笔记、$lines 条确认历史；原 JSON 未删除。历史 ts 保留为采集时间 observed_at，自动摘要保留原属性，不升级为已确认事实。"
    }
}
