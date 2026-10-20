package com.jev.probe.cloud

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.net.ServerSocket
import java.net.InetAddress
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference
import kotlin.concurrent.thread

class GatewayClientTest {
    @Test fun gatewayPreserves202GetAndDeviceHeadersWithoutRetryingOrFollowingRedirects() {
        val count = AtomicInteger()
        val failure = AtomicReference<Throwable?>()
        val server = ServerSocket(18000, 5, InetAddress.getByName("127.0.0.1"))
        val serving = thread(isDaemon = true) {
            try {
                while (!server.isClosed) {
                    server.accept().use { socket ->
                        val input = socket.getInputStream().bufferedReader()
                        val request = input.readLine()
                        val headers = linkedMapOf<String, String>()
                        while (true) { val line = input.readLine() ?: break; if (line.isBlank()) break
                            headers[line.substringBefore(':').lowercase()] = line.substringAfter(':').trim()
                        }
                        val length = headers["content-length"]?.toInt() ?: 0
                        repeat(length) { input.read() }
                        assertEquals("Bearer token", headers["authorization"])
                        assertEquals("device", headers["x-device-session"])
                        assertEquals("secret", headers["x-device-secret"])
                        count.incrementAndGet()
                        val (status, body) = when {
                            request.contains("/redirect") -> 302 to "{}"
                            request.contains("/failed") -> 503 to """{"error":{"code":"busy"}}"""
                            request.startsWith("POST") -> 202 to """{"analysis_id":"id","status":"queued"}"""
                            else -> 200 to """{"status":"succeeded"}"""
                        }
                        socket.getOutputStream().use { out ->
                            out.write(("HTTP/1.1 $status Test\r\nContent-Type: application/json\r\nContent-Length: ${body.toByteArray().size}\r\nConnection: close\r\n" +
                                (if (status == 302) "Location: /v1/analyses\r\n" else "") + "\r\n$body").toByteArray())
                        }
                    }
                }
            } catch (e: Throwable) { if (!server.isClosed) failure.set(e) }
        }
        try {
            val session = CloudSession("http://127.0.0.1:18000", "token", "u", "e", "device", "secret", "epoch")
            val client = GatewayClient(session.baseUrl, session)
            assertEquals("queued", client.request("POST", "/v1/analyses", JSONObject().put("request_id", "same")).getString("status"))
            assertEquals("succeeded", client.request("GET", "/v1/analyses/id").getString("status"))
            assertEquals("redirect_forbidden", assertThrows(CloudException::class.java) { client.request("GET", "/v1/redirect") }.code)
            assertThrows(CloudException::class.java) { client.request("POST", "/v1/failed", JSONObject()) }
            server.close(); serving.join(2000); failure.get()?.let { throw AssertionError("Test server failed", it) }
            assertEquals(4, count.get())
        } finally { server.close() }
    }
}
