package com.jev.probe.cloud

import android.app.Activity
import android.app.Application
import android.content.Context
import android.os.Bundle
import com.jev.probe.core.Analysis
import com.jev.probe.core.ChatSnapshot
import com.jev.probe.core.Prefs
import com.jev.probe.core.WorkScope
import com.jev.probe.data.ContactRepository
import com.jev.probe.sync.SyncCoordinator
import org.json.JSONObject
import java.util.UUID
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.CancellationException

class CloudApplication : Application(), Application.ActivityLifecycleCallbacks {
    private var started = 0
    override fun onCreate() { super.onCreate(); registerActivityLifecycleCallbacks(this); CloudRuntime.initialize(this) }
    override fun onActivityStarted(activity: Activity) { started++; CloudRuntime.foreground = true; CloudRuntime.schedule(this) }
    override fun onActivityStopped(activity: Activity) { started--; CloudRuntime.foreground = started > 0 }
    override fun onActivityCreated(a: Activity, b: Bundle?) = Unit
    override fun onActivityResumed(a: Activity) = Unit
    override fun onActivityPaused(a: Activity) = Unit
    override fun onActivitySaveInstanceState(a: Activity, b: Bundle) = Unit
    override fun onActivityDestroyed(a: Activity) = Unit
}

object CloudRuntime {
    private val worker = Executors.newSingleThreadScheduledExecutor()
    @Volatile var foreground = false
    @Volatile private var nextAttempt = 0L
    @Volatile private var failures = 0
    fun initialize(context: Context) {
        val app = context.applicationContext
        worker.scheduleWithFixedDelay({ if (foreground) syncQuietly(app) }, 15, 15, TimeUnit.SECONDS)
    }
    fun session(context: Context): CloudSession = SessionStore(context).load() ?: error("请先到设置登录云账号")
    fun current(context: Context, captured: CloudSession): Boolean = Prefs(context).cloudEnabled &&
        SessionStore(context).load()?.let { it.accountKey == captured.accountKey && it.deviceId == captured.deviceId } == true
    fun analysisCurrent(context: Context, analysis: Analysis, title: String?, app: String): Boolean {
        val session = SessionStore(context).load() ?: return false
        if (!Prefs(context).cloudEnabled || analysis.cloudAccount != session.accountKey || analysis.cloudDeviceId != session.deviceId) return false
        val repository = ContactRepository(context, session)
        return repository.findContact(title ?: "已确认对话", app)?.id == analysis.cloudProfileUid &&
            repository.db.record("conversations", analysis.conversationUid ?: "")?.optJSONObject("payload")?.optInt("context_revision") == analysis.contextRevision
    }
    fun schedule(context: Context) {
        val app = context.applicationContext
        worker.schedule({ if (foreground) syncQuietly(app) }, 1, TimeUnit.SECONDS)
    }
    private fun syncQuietly(context: Context) {
        if (!Prefs(context).cloudEnabled || System.currentTimeMillis() < nextAttempt) return
        val session = SessionStore(context).load() ?: return
        try { SyncCoordinator(context, session).sync(); failures = 0; nextAttempt = 0 }
        catch (_: Exception) { failures = minOf(failures + 1, 6); nextAttempt = System.currentTimeMillis() + (15000L shl failures) }
    }
}

/** Session lookup is injectable so queued lifecycle work can be verified with real SQLite. */
interface CloudSessionAccess {
    fun current(): CloudSession?
    fun revision(): Long = CloudAccountBoundary.revision()
}
class AndroidCloudSessionAccess(private val context: Context) : CloudSessionAccess {
    override fun current(): CloudSession? = if (Prefs(context).cloudEnabled) SessionStore(context).load() else null
}

class CloudAnalysis(private val context: Context,
    private val accounts: CloudSessionAccess = AndroidCloudSessionAccess(context),
    private val binding: CloudAccountBinding = CloudAccountBinding.capture(accounts)) {
    fun analyze(snapshot: ChatSnapshot, app: String, source: ChatSnapshot = snapshot, expectedProfileUid: String? = null): Analysis {
        binding.assertCurrent()
        val session = binding.session
        val repo = ContactRepository(context, session)
        val (profile, conversation) = binding.withCurrent { repo.confirm(snapshot, app, source, binding, expectedProfileUid) }
        binding.assertCurrent()
        SyncCoordinator(context, session).sync()
        check(repo.db.pendingCount() == 0) { "资料存在待上传或冲突，请到云账号页处理后再分析" }
        val revision = repo.db.record("conversations", conversation)?.getJSONObject("payload")?.getInt("context_revision")
            ?: error("对话尚未同步")
        check(CloudRuntime.current(context, session)) { "账号已变化" }
        val result = execute(binding.withCurrent { it }, conversation, revision, "analysis")
        val analysis = CloudProtocol.analysis(result).copy(cloudAccount = session.accountKey, cloudProfileUid = profile, cloudDeviceId = session.deviceId)
        check(CloudRuntime.analysisCurrent(context, analysis, snapshot.title, app)) { "人物或上下文已更新，请重新核对分析；原任务已保留" }
        return analysis
    }
    fun followup(analysis: Analysis, kind: String, selected: String? = null): JSONObject {
        val session = binding.withCurrent { it }
        check(analysis.cloudAccount == session.accountKey && analysis.cloudDeviceId == session.deviceId) { "账号或设备已切换，请重新分析" }
        val repo = ContactRepository(context, session)
        check(repo.db.record("conversations", checkNotNull(analysis.conversationUid))?.getJSONObject("payload")?.getInt("context_revision") == analysis.contextRevision) {
            "上下文版本已更新，请重新分析"
        }
        return execute(session, checkNotNull(analysis.conversationUid), checkNotNull(analysis.contextRevision), kind,
            checkNotNull(analysis.analysisId), selected)
    }
    private fun execute(session: CloudSession, conversation: String, revision: Int, kind: String,
        parent: String? = null, selected: String? = null): JSONObject {
        val db = ContactRepository(context, session).db
        val client = GatewayClient(session.baseUrl, session)
        val preferences = db.record("preferences", CloudProtocol.PREFERENCES_UID)?.optJSONObject("payload")
        val key = "$conversation|$revision|$kind|${parent.orEmpty()}|${selected.orEmpty()}|${preferences?.toString().orEmpty()}"
        binding.assertCurrent()
        val existing = db.job(key)
        existing?.optJSONObject("result")?.let { return it }
        val body = existing?.getJSONObject("body") ?: JSONObject().put("request_id", UUID.randomUUID().toString())
            .put("conversation_uid", conversation).put("context_revision", revision).put("kind", kind).apply {
                parent?.let { put("parent_analysis_id", it) }; selected?.let { put("selected_text", it) }
                preferences?.let { put("preferences", it) }
            }
        // Persist before the POST. A later explicit user retry reuses these exact bytes and request_id.
        var id = existing?.optString("analysis_id")?.takeIf { it.isNotBlank() && it != "null" }
        if (id == null) {
            binding.withCurrent { db.saveJob(key, body) }
            try {
                binding.assertCurrent()
                val created = client.request("POST", "/v1/analyses", body)
                id = created.getString("analysis_id")
                binding.withCurrent { db.saveJob(key, body, id) }
            } catch (e: Exception) {
                if (e is CancellationException || e is InterruptedException) throw e
                invalidate(session, e)
                throw IllegalStateException("任务创建未确认；请求编号已保留。再次手动分析将用同一编号查询/重放，不会自动新建付费任务。${e.message.orEmpty()}", e)
            }
        }
        repeat(120) {
            WorkScope.checkActive()
            binding.assertCurrent()
            check(CloudRuntime.current(context, session)) { "账号已切换，已停止当前显示" }
            val job = try { client.request("GET", "/v1/analyses/$id") } catch (e: Exception) { invalidate(session, e); throw e }
            check(job.getString("conversation_uid") == conversation && job.getInt("context_revision") == revision) { "任务上下文不匹配" }
            when (job.getString("status")) {
                "succeeded" -> {
                    val result = job.getJSONObject("result")
                    check(result.getString("analysis_id") == id && result.getString("conversation_uid") == conversation &&
                        result.getInt("context_revision") == revision) { "任务结果不属于当前对话版本" }
                    binding.withCurrent { db.saveJob(key, body, id, result) }
                    return result
                }
                "failed", "cancelled", "indeterminate" -> error("任务 ${job.getString("status")}：${job.optJSONObject("error")?.optString("code").orEmpty()}；保留原请求，不会自动重试模型")
            }
            Thread.sleep(1000)
        }
        error("任务仍在执行，编号已保存；稍后手动分析继续查询")
    }
    private fun invalidate(session: CloudSession, e: Exception) {
        if (e is CloudException && e.status == 401 && CloudRuntime.current(context, session)) SessionStore(context).clear()
    }
}
