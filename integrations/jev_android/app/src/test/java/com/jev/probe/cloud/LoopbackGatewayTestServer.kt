package com.jev.probe.cloud

import java.io.IOException
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Proxy
import java.net.ProxySelector
import java.net.ServerSocket
import java.net.SocketAddress
import java.net.URI

/** Keep the production localhost:18000 rule while avoiding any real service on that port. */
internal class LoopbackGatewayTestServer : ServerSocket(0, 5, InetAddress.getByName("127.0.0.1")) {
    private val previousSelector = ProxySelector.getDefault()
    init {
        ProxySelector.setDefault(object : ProxySelector() {
            override fun select(uri: URI): List<Proxy> {
                check(uri.scheme == "http" && uri.host in listOf("127.0.0.1", "localhost") && uri.port == 18000) {
                    "Unexpected URL in isolated gateway test"
                }
                return listOf(Proxy(Proxy.Type.HTTP, InetSocketAddress("127.0.0.1", this@LoopbackGatewayTestServer.localPort)))
            }
            override fun connectFailed(uri: URI?, address: SocketAddress?, error: IOException?) = Unit
        })
    }
    override fun close() {
        ProxySelector.setDefault(previousSelector)
        super.close()
    }
}
