package com.jev.probe.sync

import android.content.Context
import com.jev.probe.cloud.*
import com.jev.probe.data.LocalDatabase
import org.json.JSONObject
import java.io.File
import java.net.URLEncoder
import java.util.concurrent.ConcurrentHashMap

class SyncCoordinator(context: Context, val session: CloudSession) {
    private val app = context.applicationContext
    val database = LocalDatabase.get(app, session.accountKey)
    private val lock = locks.getOrPut(session.accountKey) { Any() }
    fun sync() = synchronized(lock) {
        check(database.meta("recovery_required") != "true") { "恢复副本待处理：请到云账号页确认重建" }
        var client = GatewayClient(session.baseUrl, session)
        try {
            checkCurrent()
            if (System.currentTimeMillis() - (database.meta("token_refreshed")?.toLongOrNull() ?: 0L) > 12 * 60 * 60 * 1000L) {
                val token = client.request("POST", "/v1/auth/refresh").getString("token")
                checkCurrent()
                val refreshed = session.copy(token = token)
                SessionStore(app).save(refreshed)
                client = GatewayClient(refreshed.baseUrl, refreshed)
                database.setMeta("token_refreshed", System.currentTimeMillis().toString())
            }
            val config = client.request("GET", "/v1/config")
            require(config.getInt("api_version") == 1 && config.getInt("min_client_version") <= 1) { "需要更新客户端" }
            check(config.getBoolean("sync_ready")) { "服务器同步尚未就绪" }
            checkEpoch(config.getString("server_epoch"))
            // Pull before pushing so unrelated remote changes are never skipped by a push seq.
            pull(client)
            var rounds = 0
            while (rounds++ < 100) {
                val batch = database.batch()
                if (batch.length() == 0) break
                checkCurrent()
                val result = client.request("POST", "/v1/sync/push", JSONObject()
                    .put("server_epoch", session.epoch).put("operations", batch))
                checkEpoch(result.getString("server_epoch"))
                database.receipts(result.getJSONArray("results"))
                // Conflicts/rejections block their descendants; unrelated pending data may continue.
            }
            pull(client)
            database.setMeta("sync_status", if (database.conflicts().isNotEmpty()) "存在冲突" else if (database.pendingCount() > 0) "存在待处理操作" else "已同步")
        } catch (e: CloudException) {
            if (e.status == 401 || e.code == "server_epoch_changed") {
                if (e.code == "server_epoch_changed") preserveRecovery()
                val vault = SessionStore(app)
                if (vault.load()?.deviceId == session.deviceId) vault.clear()
            }
            database.setMeta("sync_status", e.message ?: "同步失败")
            throw e
        } catch (e: Exception) {
            database.setMeta("sync_status", "离线或同步失败；待上传内容已保留")
            throw e
        }
    }
    private fun pull(client: GatewayClient) {
        var more: Boolean
        var pages = 0
        do {
            checkCurrent()
            check(pages++ < 1000) { "同步分页超过上限，请稍后继续" }
            val cursor = database.meta("cursor").orEmpty()
            val page = client.request("GET", "/v1/sync/pull?limit=200" +
                if (cursor.isBlank()) "" else "&cursor=" + URLEncoder.encode(cursor, "UTF-8"))
            checkEpoch(page.getString("server_epoch"))
            database.applyPage(page)
            more = page.getBoolean("has_more")
        } while (more)
    }
    private fun checkEpoch(epoch: String) {
        val previous = database.meta("server_epoch")
        if (epoch != session.epoch) {
            preserveRecovery()
            throw CloudException(409, "server_epoch_changed")
        }
        if (previous != null && previous != epoch) {
            preserveRecovery()
            error("本地资料属于备份恢复前的服务器世代，请在云账号页保存副本并确认重建")
        }
        database.setMeta("server_epoch", epoch)
    }
    private fun checkCurrent() { check(CloudRuntime.current(app, session)) { "账号或运行方式已切换，已停止同步" } }
    private fun preserveRecovery() {
        database.exportRecovery(File(app.filesDir, "cloud-recovery"))
        database.setMeta("recovery_required", "true")
    }
    companion object { private val locks = ConcurrentHashMap<String, Any>() }
}
