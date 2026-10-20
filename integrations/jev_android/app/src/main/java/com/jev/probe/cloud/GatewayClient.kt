package com.jev.probe.cloud

import com.jev.probe.core.WorkScope
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

class CloudException(val status: Int, val code: String) : RuntimeException(when {
    status == 401 -> "登录已失效，请重新登录（本地待上传内容已保留）"
    code == "server_epoch_changed" -> "服务器已恢复备份；本地恢复副本已保留，请重新登录并处理差异"
    status == 429 -> "服务繁忙，请稍后手动重试"
    else -> "云服务请求失败：$code（HTTP $status）"
})

/** Deliberately one attempt: ambiguous generation POSTs must retain their request ID. */
class GatewayClient(base: String, private val session: CloudSession? = null) {
    val baseUrl = CloudProtocol.baseUrl(base)
    init { require(session == null || session.baseUrl == baseUrl) { "登录会话与服务器不匹配，请重新登录" } }
    fun request(method: String, path: String, body: JSONObject? = null): JSONObject {
        require(path.startsWith("/v1/"))
        WorkScope.checkActive()
        val connection = (URL(baseUrl + path).openConnection() as HttpURLConnection).apply {
            requestMethod = method; instanceFollowRedirects = false
            connectTimeout = 15000; readTimeout = 45000
            setRequestProperty("Accept", "application/json")
            session?.let {
                setRequestProperty("Authorization", "Bearer ${it.token}")
                setRequestProperty("X-Device-Session", it.deviceId)
                setRequestProperty("X-Device-Secret", it.deviceSecret)
            }
        }
        val detach = WorkScope.onCancel { connection.disconnect() }
        try {
            if (body != null) {
                connection.doOutput = true
                connection.setRequestProperty("Content-Type", "application/json; charset=utf-8")
                connection.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            }
            val status = connection.responseCode
            if (status in 300..399) throw CloudException(status, "redirect_forbidden")
            val stream = if (status in 200..299) connection.inputStream else connection.errorStream
            val raw = stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() }.orEmpty()
            val json = if (raw.isBlank()) JSONObject() else try { JSONObject(raw) }
                catch (_: Exception) { throw CloudException(status, "invalid_json") }
            if (status !in 200..299) throw CloudException(status, json.optJSONObject("error")?.optString("code") ?: "http_error")
            WorkScope.checkActive()
            return json
        } finally { detach(); connection.disconnect() }
    }
    fun login(email: String, password: String, device: String): CloudSession {
        val o = request("POST", "/v1/auth/login", JSONObject().put("email", email).put("password", password)
            .put("device_name", device).put("platform", "android"))
        require(o.getInt("api_version") == 1) { "服务器版本不兼容" }
        return CloudSession(baseUrl, o.getString("token"), o.getJSONObject("user").getString("id"),
            o.getJSONObject("user").getString("email"), o.getJSONObject("device").getString("id"),
            o.getString("device_secret"), o.getString("server_epoch"))
    }
}
