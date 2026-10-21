package com.jev.probe.cloud

import com.jev.probe.core.ChatSnapshot
import java.util.concurrent.CancellationException
import java.util.UUID

/** Authentication changes and short local transactions share this boundary. Never hold it over HTTP. */
object CloudAccountBoundary {
    val lock = Any()
    private var revision = 0L
    private val observers = LinkedHashSet<() -> Unit>()
    fun revision(): Long = synchronized(lock) { revision }
    fun changed() = synchronized(lock) { revision++; observers.toList().forEach { it() } }
    fun observe(callback: () -> Unit): () -> Unit = synchronized(lock) {
        observers.add(callback)
        return@synchronized { synchronized(lock) { observers.remove(callback) }; Unit }
    }
}

/** Captured while opening a review, never re-bound to whoever is logged in when a worker runs. */
class CloudAccountBinding private constructor(val session: CloudSession, private val revision: Long,
    private val accounts: CloudSessionAccess) {
    fun assertCurrent() = synchronized(CloudAccountBoundary.lock) {
        val current = accounts.current()
        if (current == null || current.accountKey != session.accountKey || current.deviceId != session.deviceId ||
            accounts.revision() != revision) throw CancellationException("云账号或设备已切换，请重新核对原文")
    }
    fun <T> withCurrent(block: (CloudSession) -> T): T = synchronized(CloudAccountBoundary.lock) {
        assertCurrent()
        block(checkNotNull(accounts.current())) // may contain a refreshed token, same account/device only
    }
    companion object {
        fun capture(accounts: CloudSessionAccess): CloudAccountBinding = synchronized(CloudAccountBoundary.lock) {
            CloudAccountBinding(checkNotNull(accounts.current()) { "请先登录云账号" }, accounts.revision(), accounts)
        }
    }
}

data class CloudReview(val binding: CloudAccountBinding, val source: ChatSnapshot, val app: String,
    val profileUid: String? = null, val choice: CloudReviewChoice? = null,
    val reviewUid: String = UUID.randomUUID().toString())
