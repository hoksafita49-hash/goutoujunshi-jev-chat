package com.jev.probe.data

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import com.jev.probe.cloud.CloudProtocol
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.UUID

/** One physical SQLite file per server/account. All visible edits and their outbox are atomic. */
class LocalDatabase private constructor(context: Context, val account: String) : SQLiteOpenHelper(context,
    "cloud_${CloudProtocol.legacyUid(account, "database")}.db", null, 1) {
    override fun onConfigure(db: SQLiteDatabase) { db.setForeignKeyConstraintsEnabled(true) }
    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("CREATE TABLE records(type TEXT NOT NULL,uid TEXT NOT NULL,remote TEXT,local TEXT,PRIMARY KEY(type,uid))")
        db.execSQL("CREATE TABLE outbox(n INTEGER PRIMARY KEY AUTOINCREMENT,op_id TEXT UNIQUE NOT NULL,type TEXT NOT NULL,uid TEXT NOT NULL,base_version INTEGER NOT NULL,action TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL)")
        db.execSQL("CREATE TABLE conflicts(op_id TEXT PRIMARY KEY,type TEXT NOT NULL,uid TEXT NOT NULL,local TEXT NOT NULL,remote TEXT NOT NULL)")
        db.execSQL("CREATE TABLE meta(k TEXT PRIMARY KEY,v TEXT NOT NULL)")
        db.execSQL("CREATE TABLE legacy_id_map(source TEXT PRIMARY KEY,uid TEXT NOT NULL)")
        db.execSQL("CREATE TABLE jobs(k TEXT PRIMARY KEY,body TEXT NOT NULL,analysis_id TEXT,result TEXT)")
    }
    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) = Unit
    @Synchronized fun <T> transaction(block: (SQLiteDatabase) -> T): T {
        val db = writableDatabase
        db.beginTransaction()
        return try { val result = block(db); db.setTransactionSuccessful(); result } finally { db.endTransaction() }
    }
    @Synchronized fun meta(key: String): String? = readableDatabase.rawQuery("SELECT v FROM meta WHERE k=?", arrayOf(key)).use {
        if (it.moveToFirst()) it.getString(0) else null
    }
    fun setMeta(key: String, value: String) = transaction { db -> meta(db, key, value) }
    private fun meta(db: SQLiteDatabase, key: String, value: String) {
        db.insertWithOnConflict("meta", null, ContentValues().apply { put("k", key); put("v", value) }, SQLiteDatabase.CONFLICT_REPLACE)
    }
    private fun row(db: SQLiteDatabase, type: String, uid: String, column: String): JSONObject? =
        db.rawQuery("SELECT $column FROM records WHERE type=? AND uid=?", arrayOf(type, uid)).use {
            if (it.moveToFirst() && !it.isNull(0)) JSONObject(it.getString(0)) else null
        }
    @Synchronized fun record(type: String, uid: String): JSONObject? {
        val db = readableDatabase
        val remote = row(db, type, uid, "remote")
        if (remote != null && !remote.isNull("deleted_at")) return remote
        return row(db, type, uid, "local") ?: remote
    }
    @Synchronized fun records(type: String): List<JSONObject> = readableDatabase.rawQuery(
        "SELECT local,remote FROM records WHERE type=? ORDER BY rowid", arrayOf(type)).use { c ->
        buildList { while (c.moveToNext()) {
            val remote = if (c.isNull(1)) null else JSONObject(c.getString(1))
            val visible = if (c.isNull(0)) remote else JSONObject(c.getString(0))
            if (visible != null && visible.isNull("deleted_at") && (remote == null || remote.isNull("deleted_at"))) add(visible)
        } }
    }
    fun edit(type: String, uid: String, payload: JSONObject, delete: Boolean = false) = transaction { db ->
        check(meta("recovery_required") != "true") { "服务器恢复待处理：请先导出本地恢复副本并确认重建" }
        edit(db, type, uid, payload, delete)
    }
    /** Caller may use this inside an import transaction. Payload is copied, never mutated after enqueue. */
    fun edit(db: SQLiteDatabase, type: String, uid: String, payload: JSONObject, delete: Boolean = false) {
        check(meta("recovery_required") != "true") { "服务器恢复待处理：请先保留副本并确认重建" }
        require(type != "analyses") { "分析结果只能由服务端写入" }
        val remote = row(db, type, uid, "remote")
        require(remote == null || remote.isNull("deleted_at")) { "对象已删除，不能复活" }
        val local = row(db, type, uid, "local")
        val now = CloudProtocol.now()
        val rec = JSONObject().put("entity_type", type).put("uid", uid)
            .put("version", remote?.optInt("version") ?: 0)
            .put("created_at", remote?.optString("created_at") ?: local?.optString("created_at") ?: now)
            .put("updated_at", now).put("deleted_at", if (delete) now else JSONObject.NULL).put("payload", payload)
        db.execSQL("INSERT OR IGNORE INTO records(type,uid) VALUES(?,?)", arrayOf(type, uid))
        db.update("records", ContentValues().apply { put("local", rec.toString()) }, "type=? AND uid=?", arrayOf(type, uid))
        // Only unsent operations may be coalesced. An ambiguous/sent op retains its original bytes and ID.
        db.delete("outbox", "type=? AND uid=? AND state='pending'", arrayOf(type, uid))
        db.insertOrThrow("outbox", null, ContentValues().apply {
            put("op_id", UUID.randomUUID().toString()); put("type", type); put("uid", uid)
            put("base_version", remote?.optInt("version") ?: 0); put("action", if (delete) "delete" else "upsert")
            put("payload", payload.toString()); put("state", "pending")
        })
    }
    fun batch(): JSONArray = transaction { db ->
        val a = JSONArray()
        db.rawQuery("SELECT op_id,type,uid,base_version,action,payload FROM outbox o WHERE state IN ('pending','sending') AND NOT EXISTS (SELECT 1 FROM outbox p WHERE p.type=o.type AND p.uid=o.uid AND p.n<o.n) ORDER BY n LIMIT 50", null).use { c ->
            while (c.moveToNext()) {
                a.put(JSONObject().put("op_id", c.getString(0)).put("entity_type", c.getString(1))
                    .put("entity_uid", c.getString(2)).put("base_version", c.getLong(3)).put("action", c.getString(4))
                    .put("payload", JSONObject(c.getString(5))))
                db.execSQL("UPDATE outbox SET state='sending' WHERE op_id=?", arrayOf(c.getString(0)))
            }
        }
        a
    }
    fun receipts(results: JSONArray) = transaction { db ->
        for (i in 0 until results.length()) {
            val result = results.getJSONObject(i); val id = result.getString("op_id")
            db.rawQuery("SELECT type,uid,payload FROM outbox WHERE op_id=?", arrayOf(id)).use { c ->
                if (c.moveToFirst()) {
                    val type = c.getString(0); val uid = c.getString(1); val rec = result.optJSONObject("record")
                    when (result.getString("status")) {
                        "applied" -> {
                            require(rec != null) { "确认响应缺少 record" }
                            mirror(db, rec)
                            db.delete("outbox", "op_id=?", arrayOf(id))
                            db.execSQL("UPDATE outbox SET base_version=? WHERE type=? AND uid=? AND state='pending'", arrayOf(rec.getInt("version"), type, uid))
                            val remaining = db.rawQuery("SELECT 1 FROM outbox WHERE type=? AND uid=? LIMIT 1", arrayOf(type, uid)).use { it.moveToFirst() }
                            if (!remaining) db.execSQL("UPDATE records SET local=NULL WHERE type=? AND uid=?", arrayOf(type, uid))
                        }
                        "conflict" -> {
                            require(rec != null) { "冲突响应缺少 record" }; mirror(db, rec)
                            db.execSQL("UPDATE outbox SET state='conflict' WHERE op_id=?", arrayOf(id))
                            db.insertWithOnConflict("conflicts", null, ContentValues().apply {
                                put("op_id", id); put("type", type); put("uid", uid)
                                put("local", row(db, type, uid, "local")?.toString() ?: c.getString(2)); put("remote", rec.toString())
                            }, SQLiteDatabase.CONFLICT_REPLACE)
                        }
                        else -> {
                            if (rec != null) mirror(db, rec)
                            db.execSQL("UPDATE outbox SET state='rejected' WHERE op_id=?", arrayOf(id))
                            meta(db, "outbox_error/$id", result.optString("code", "unknown_rejection"))
                        }
                    }
                }
            }
        }
    }
    private fun mirror(db: SQLiteDatabase, record: JSONObject) {
        for (key in listOf("entity_type", "uid", "version", "created_at", "updated_at", "deleted_at", "payload"))
            require(record.has(key)) { "同步记录缺少 $key" }
        require(record.getInt("version") > 0 && record.optJSONObject("payload") != null) { "同步记录版本或内容无效" }
        val type = record.getString("entity_type"); val uid = record.getString("uid")
        val old = row(db, type, uid, "remote")
        if (old != null && old.optInt("version") > record.getInt("version")) return
        db.execSQL("INSERT OR IGNORE INTO records(type,uid) VALUES(?,?)", arrayOf(type, uid))
        db.update("records", ContentValues().apply { put("remote", record.toString()) }, "type=? AND uid=?", arrayOf(type, uid))
    }
    fun applyPage(page: JSONObject) = transaction { db ->
        val changes = page.getJSONArray("changes")
        for (i in 0 until changes.length()) mirror(db, changes.getJSONObject(i).getJSONObject("record"))
        meta(db, "cursor", page.getString("cursor"))
        meta(db, "last_sync", CloudProtocol.now())
    }
    @Synchronized fun pendingCount(): Int = readableDatabase.rawQuery("SELECT COUNT(*) FROM outbox", null).use { it.moveToFirst(); it.getInt(0) }
    @Synchronized fun rejectedOperations(): List<JSONObject> = readableDatabase.rawQuery(
        "SELECT op_id,type,uid,base_version,action,payload FROM outbox WHERE state='rejected' ORDER BY n", null).use { c ->
        buildList { while (c.moveToNext()) add(JSONObject().put("op_id", c.getString(0)).put("entity_type", c.getString(1))
            .put("entity_uid", c.getString(2)).put("base_version", c.getInt(3)).put("action", c.getString(4))
            .put("payload", JSONObject(c.getString(5))).put("code", meta("outbox_error/${c.getString(0)}") ?: "unknown_rejection")) }
    }
    /** Explicit text-sync recovery only; never called by periodic sync or any model-job retry. */
    fun requeueRejectedDependencies(): Int = transaction { db ->
        check(meta("recovery_required") != "true") { "请先处理服务器备份恢复" }
        val all = ArrayList<JSONObject>()
        db.rawQuery("SELECT n,op_id,type,uid,base_version,action,payload,state FROM outbox ORDER BY n", null).use { c ->
            while (c.moveToNext()) all.add(JSONObject().put("n", c.getLong(0)).put("op_id", c.getString(1))
                .put("entity_type", c.getString(2)).put("entity_uid", c.getString(3)).put("base_version", c.getInt(4))
                .put("action", c.getString(5)).put("payload", JSONObject(c.getString(6))).put("state", c.getString(7)))
        }
        val held = all.filter { it.getString("state") == "rejected" &&
            meta("outbox_error/${it.getString("op_id")}") == "dependency_failed" }
        if (held.isEmpty()) return@transaction 0
        val selected = held.map { it.getString("op_id") }.toSet()
        fun sameObject(a: JSONObject, b: JSONObject) = a.getString("entity_type") == b.getString("entity_type") &&
            a.getString("entity_uid") == b.getString("entity_uid")
        fun validate(type: String, uid: String, parent: Boolean) {
            val remote = row(db, type, uid, "remote")
            val local = row(db, type, uid, "local")
            check(remote == null || remote.isNull("deleted_at")) { "服务端已删除 $type/$uid，禁止复活；草稿仍保留" }
            if (parent) check((local ?: remote)?.isNull("deleted_at") == true) { "父对象不存在或已删除：$type/$uid" }
            check(all.none { it.getString("entity_type") == type && it.getString("entity_uid") == uid &&
                it.getString("state") in listOf("conflict", "rejected") && it.getString("op_id") !in selected }) {
                "请先解决父对象或当前对象的冲突/拒绝：$type/$uid"
            }
        }
        for (op in held) {
            val type = op.getString("entity_type"); val p = op.getJSONObject("payload")
            check(type != "analyses") { "服务端分析结果不能由草稿重排" }
            validate(type, op.getString("entity_uid"), false)
            if (type in listOf("conversations", "messages", "memories", "identity_links"))
                validate("profiles", p.getString("profile_uid"), true)
            if (type == "messages") validate("conversations", p.getString("conversation_uid"), true)
            if (type == "feedback") validate("analyses", p.getString("analysis_uid"), true)
        }
        // Move later unsent edits on those same objects together, preserving old->new payload order.
        val moved = all.filter { op -> op.getString("op_id") in selected || (op.getString("state") == "pending" &&
            held.any { sameObject(it, op) && it.getLong("n") < op.getLong("n") }) }
        for (op in moved) db.delete("outbox", "op_id=?", arrayOf(op.getString("op_id")))
        for (op in moved) {
            val oldId = op.getString("op_id")
            val nextId = if (oldId in selected) UUID.randomUUID().toString() else oldId
            db.insertOrThrow("outbox", null, ContentValues().apply {
                put("op_id", nextId); put("type", op.getString("entity_type")); put("uid", op.getString("entity_uid"))
                put("base_version", op.getInt("base_version")); put("action", op.getString("action"))
                put("payload", op.getJSONObject("payload").toString()); put("state", "pending")
            })
            if (oldId in selected) meta(db, "outbox_requeue/$oldId", JSONObject().put("new_op_id", nextId)
                .put("code", "dependency_failed").put("at", CloudProtocol.now()).toString())
        }
        held.size
    }
    @Synchronized fun conflicts(): List<JSONObject> = readableDatabase.rawQuery("SELECT op_id,type,uid,local,remote FROM conflicts", null).use { c ->
        buildList { while (c.moveToNext()) add(JSONObject().put("op_id", c.getString(0)).put("entity_type", c.getString(1))
            .put("uid", c.getString(2)).put("local", JSONObject(c.getString(3))).put("remote", JSONObject(c.getString(4)))) }
    }
    fun resolve(id: String, useLocal: Boolean) = transaction { db ->
        val conflict = conflicts().first { it.getString("op_id") == id }
        val type = conflict.getString("entity_type"); val uid = conflict.getString("uid")
        db.delete("outbox", "type=? AND uid=?", arrayOf(type, uid))
        db.delete("conflicts", "type=? AND uid=?", arrayOf(type, uid))
        val local = row(db, type, uid, "local")
        db.execSQL("UPDATE records SET local=NULL WHERE type=? AND uid=?", arrayOf(type, uid))
        if (useLocal) {
            require(conflict.getJSONObject("remote").isNull("deleted_at")) { "服务端已删除；原文仍在冲突中，可先导出恢复副本，不能复活" }
            edit(db, type, uid, local?.getJSONObject("payload") ?: conflict.getJSONObject("local").getJSONObject("payload"))
        }
    }
    fun legacyUid(source: String, db: SQLiteDatabase = writableDatabase): String {
        db.rawQuery("SELECT uid FROM legacy_id_map WHERE source=?", arrayOf(source)).use { if (it.moveToFirst()) return it.getString(0) }
        return CloudProtocol.legacyUid(account, source).also { uid ->
            db.insertOrThrow("legacy_id_map", null, ContentValues().apply { put("source", source); put("uid", uid) })
        }
    }
    @Synchronized fun job(key: String): JSONObject? = readableDatabase.rawQuery("SELECT body,analysis_id,result FROM jobs WHERE k=?", arrayOf(key)).use {
        if (!it.moveToFirst()) null else JSONObject().put("body", JSONObject(it.getString(0)))
            .put("analysis_id", if (it.isNull(1)) JSONObject.NULL else it.getString(1))
            .put("result", if (it.isNull(2)) JSONObject.NULL else JSONObject(it.getString(2)))
    }
    fun saveJob(key: String, body: JSONObject, analysisId: String? = null, result: JSONObject? = null) = transaction { db ->
        db.insertWithOnConflict("jobs", null, ContentValues().apply {
            put("k", key); put("body", body.toString()); put("analysis_id", analysisId); put("result", result?.toString())
        }, SQLiteDatabase.CONFLICT_REPLACE)
    }
    @Synchronized fun jobs(): List<JSONObject> = readableDatabase.rawQuery("SELECT k,body,analysis_id,result FROM jobs ORDER BY rowid DESC", null).use { c ->
        buildList { while (c.moveToNext()) add(JSONObject().put("key", c.getString(0)).put("body", JSONObject(c.getString(1)))
            .put("analysis_id", if (c.isNull(2)) JSONObject.NULL else c.getString(2)).put("completed", !c.isNull(3))) }
    }
    /** Explicit user choice only. Never called as an automatic retry or timeout handler. */
    fun allowNewJob(key: String) = transaction { db ->
        val previous = checkNotNull(job(key))
        meta(db, "previous_job/${UUID.randomUUID()}", previous.toString())
        val body = previous.getJSONObject("body").put("request_id", UUID.randomUUID().toString())
        db.execSQL("UPDATE jobs SET body=?,analysis_id=NULL,result=NULL WHERE k=?", arrayOf(body.toString(), key))
    }
    /** Logical export includes unsent edits, conflicts and task request IDs without credentials. */
    fun exportRecovery(directory: File): File = transaction { db ->
        directory.mkdirs()
        val export = JSONObject().put("account", account).put("exported_at", CloudProtocol.now())
        for (table in listOf("records", "outbox", "conflicts", "meta", "legacy_id_map", "jobs")) {
            val rows = JSONArray()
            db.rawQuery("SELECT * FROM $table", null).use { c -> while (c.moveToNext()) {
                val row = JSONObject(); c.columnNames.forEachIndexed { i, name -> row.put(name, if (c.isNull(i)) JSONObject.NULL else c.getString(i)) }; rows.put(row)
            } }
            export.put(table, rows)
        }
        File(directory, "recovery-${System.currentTimeMillis()}.json").also { it.writeText(export.toString(2)) }
    }
    fun acceptRebuild(directory: File) {
        exportRecovery(directory)
        transaction { db ->
            for (table in listOf("records", "outbox", "conflicts", "meta", "legacy_id_map", "jobs")) db.delete(table, null, null)
        }
    }
    companion object {
        private val instances = HashMap<String, LocalDatabase>()
        @Synchronized fun get(context: Context, account: String): LocalDatabase =
            instances.getOrPut(account) { LocalDatabase(context.applicationContext, account) }
    }
}
