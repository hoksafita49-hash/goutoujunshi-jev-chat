package com.jev.probe

import android.os.Build
import android.os.Bundle
import android.text.InputType
import android.widget.*
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import com.jev.probe.cloud.*
import com.jev.probe.core.Prefs
import com.jev.probe.data.LocalDatabase
import com.jev.probe.data.ContactRepository
import com.jev.probe.sync.LegacyImporter
import com.jev.probe.sync.SyncCoordinator
import org.json.JSONObject
import java.io.File
import java.util.concurrent.Executors

/** Account, migration and conflict choices remain in the main app, outside capture overlays. */
class CloudAccountActivity : AppCompatActivity() {
    private val worker = Executors.newSingleThreadExecutor()
    private lateinit var root: LinearLayout
    private lateinit var status: TextView
    private var busy = false
    override fun onCreate(savedInstanceState: Bundle?) { super.onCreate(savedInstanceState); render() }
    private fun render() {
        root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(28, 30, 28, 40) }
        root.padForSystemBars()
        setContentView(ScrollView(this).apply { addView(root) })
        label("云账号与三端同步", 23f)
        val prefs = Prefs(this)
        label("账号、同步和分析都经过同一个网关。云模式仅使用本机 OCR；不会读取设备里的供应商密钥。")
        status = label("")
        val session = SessionStore(this).load()
        if (session == null) {
            label("服务器地址（HTTPS；开发可 adb reverse tcp:18000 tcp:18000）")
            val base = field(prefs.cloudBaseUrl)
            label("账号邮箱"); val email = field("")
            label("密码"); val password = field("", true)
            label("设备名"); val device = field("${Build.MANUFACTURER} ${Build.MODEL}")
            button("登录并使用云模式") {
                val baseValue = base.text.toString(); val emailValue = email.text.toString().trim()
                val passwordValue = password.text.toString(); val name = device.text.toString()
                password.text.clear()
                runWork {
                    val normalized = CloudProtocol.baseUrl(baseValue)
                    check(BuildConfig.DEBUG || normalized.startsWith("https://")) { "发布版仅允许 HTTPS" }
                    val loggedIn = GatewayClient(normalized).login(emailValue, passwordValue, name)
                    SessionStore(this).save(loggedIn); prefs.cloudBaseUrl = normalized; prefs.cloudEnabled = true
                    "登录成功；旧 JSON 需预览确认后才会导入"
                }
            }
            return
        }
        label("${session.email}\n${session.baseUrl}\n当前设备：${session.deviceId}")
        val chatTitle = intent.getStringExtra("chat_title")
        val chatApp = intent.getStringExtra("chat_app")
        if (!chatTitle.isNullOrBlank() && !chatApp.isNullOrBlank()) {
            label("待关联聊天：$chatTitle")
            button("将当前聊天关联至已有云人物") {
                val repository = ContactRepository(this, session); val contacts = repository.contacts()
                if (contacts.isEmpty()) showText("尚无云人物", "先同步其他设备创建的人物，或者明确创建一个新人。")
                else AlertDialog.Builder(this).setTitle("明确选择同一个人")
                    .setItems(contacts.map { "${it.name} · ${it.stage}" }.toTypedArray()) { _, n -> runWork {
                        check(CloudRuntime.current(this, session)) { "账号已变化" }
                        repository.bindContact(contacts[n].id, chatTitle, chatApp); CloudRuntime.schedule(this); "关联已保存，返回聊天后核对分析"
                    } }.setNegativeButton("取消", null).show()
            }
            button("明确创建一个新的云人物") { runWork {
                check(CloudRuntime.current(this, session)) { "账号已变化" }
                ContactRepository(this, session).saveOrMergeContact(chatTitle, chatApp).also { CloudRuntime.schedule(this) }
            } }
        }
        val db = LocalDatabase.get(this, session.accountKey)
        val rejected = db.rejectedOperations()
        label("${db.meta("sync_status") ?: "尚未同步"}\n待处理：${db.pendingCount()}；冲突：${db.conflicts().size}；拒绝：${rejected.size}\n最后同步：${db.meta("last_sync") ?: "无"}")
        if (rejected.isNotEmpty()) label("拒绝原因：" + rejected.groupingBy { it.getString("code") }.eachCount()
            .entries.joinToString("；") { "${it.key} ${it.value} 条" })
        button("立即同步 / 恢复任务结果") { runWork { SyncCoordinator(this, session).sync(); "同步完成" } }
        button("刷新登录凭据") { runWork {
            val refreshed = GatewayClient(session.baseUrl, session).request("POST", "/v1/auth/refresh")
            check(SessionStore(this).load()?.deviceId == session.deviceId) { "账号已变化" }
            SessionStore(this).save(session.copy(token = refreshed.getString("token")))
            "登录凭据已刷新"
        } }
        button("预览本机旧 JSON 并确认导入") { runWorkUi {
            val importer = LegacyImporter(this, session); val preview = importer.preview()
            runOnUiThread {
                val text = TextView(this).apply { setPadding(24, 12, 24, 12); this.text = preview; setTextIsSelectable(true) }
                AlertDialog.Builder(this).setTitle("只导入当前账号；核对原文与人物")
                    .setView(ScrollView(this).apply { addView(text) }).setNegativeButton("取消", null)
                    .setPositiveButton("确认原文并导入") { _, _ -> runWork {
                        check(CloudRuntime.current(this, session)) { "账号已切换" }
                        importer.importConfirmed().also { CloudRuntime.schedule(this) }
                    } }.show()
            }
        } }
        button("查看与处理冲突") { showConflicts(db) }
        button("被拒草稿 · 查看错误与手动重新排队") { showRejected(db, session) }
        button("跨端回复偏好") {
            val binding = try { CloudAccountBinding.capture(AndroidCloudSessionAccess(this)) }
                catch (e: Exception) { Toast.makeText(this, e.message ?: "请先启用云模式并登录", Toast.LENGTH_LONG).show(); return@button }
            val preferences = db.record("preferences", CloudProtocol.PREFERENCES_UID)?.optJSONObject("payload") ?: JSONObject()
            val box = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(24, 12, 24, 12) }
            fun input(label: String, value: String): EditText {
                box.addView(TextView(this).apply { text = label })
                return EditText(this).apply { setText(value); box.addView(this) }
            }
            val tone = input("口吻", preferences.optString("tone", "自然"))
            val length = input("回复长度", preferences.optString("length", "适中"))
            val count = input("候选数量（1–3）", preferences.optInt("count", 3).toString())
            val style = input("个人表达习惯", preferences.optString("style"))
            AlertDialog.Builder(this).setTitle("同一账号共享偏好").setView(box).setNegativeButton("取消", null)
                .setPositiveButton("保存并同步") { _, _ ->
                    val toneValue = tone.text.toString(); val lengthValue = length.text.toString()
                    val countValue = count.text.toString().toIntOrNull() ?: 3; val styleValue = style.text.toString()
                    runWork {
                        binding.withCurrent {
                            check(it.accountKey == session.accountKey && it.deviceId == session.deviceId) { "账号已切换" }
                            db.transaction { sql ->
                                val latest = db.record("preferences", CloudProtocol.PREFERENCES_UID)?.optJSONObject("payload") ?: JSONObject()
                                db.edit(sql, "preferences", CloudProtocol.PREFERENCES_UID, EvidenceMemoryPreferences.save(latest, toneValue, lengthValue, countValue, styleValue))
                            }
                        }
                        CloudRuntime.schedule(this); "偏好已保存到待上传队列"
                    }
                }.show()
        }
        button("查看已同步分析") {
            val analyses = db.records("analyses")
            AlertDialog.Builder(this).setTitle("已同步分析 ${analyses.size} 条")
                .setItems(analyses.map { it.getString("uid") }.toTypedArray()) { _, which ->
                    CloudEvidenceMemoryActivity.open(this, session, analyses[which].getString("uid"))
                }.setNegativeButton("关闭", null).show()
        }
        button("人物记忆 · 核对、修改与历史") { CloudEvidenceMemoryActivity.open(this, session) }
        button("任务记录与手动重新请求") {
            val jobs = db.jobs()
            AlertDialog.Builder(this).setTitle("再次分析默认继续原任务")
                .setItems(jobs.map { "${it.getJSONObject("body").optString("kind")} · ${it.optString("analysis_id")} ${if (it.optBoolean("local_cancelled")) "原文已删除 · 已取消" else if (it.getBoolean("completed")) "已完成" else "结果待查询"}" }.toTypedArray()) { _, n ->
                    val job = jobs[n]
                    if (job.optBoolean("local_cancelled")) {
                        showText("原分析或来源已删除", "已清除正文，只保留请求编号等取消记录用于阻止重复提交。此任务不能恢复或重新请求；请核对新的对话上下文。")
                        return@setItems
                    }
                    AlertDialog.Builder(this).setTitle("明确允许新的付费请求")
                        .setMessage("原请求编号与结果会保留在恢复导出中。若上游已接受但响应丢失，新请求可能再次计费。取消后，下次分析仍继续查询原任务。")
                        .setNegativeButton("保留原任务", null).setPositiveButton("允许新请求") { _, _ -> runWork {
                            check(CloudRuntime.current(this, session)) { "账号已变化" }
                            db.allowNewJob(job.getString("key")); "已准备新的请求编号；返回原对话后手动分析才会提交"
                        } }.show()
                }.setNegativeButton("关闭", null).show()
        }
        button("导出完整本地恢复副本") { runWork {
            val file = db.exportRecovery(File(getExternalFilesDir(null), "cloud-recovery"))
            "已导出：${file.absolutePath}（包含聊天，请自行保管）"
        } }
        if (db.meta("recovery_required") == "true") button("备份恢复：保存副本并重建云镜像") {
            AlertDialog.Builder(this).setTitle("保存原数据后重新下载")
                .setMessage("将先导出当前账号的完整恢复 JSON，再重建缓存并从服务器下载。未上传和比备份更新的数据只保留在恢复文件中，需要你核对后人工恢复；不会自动回灌旧数据。")
                .setNegativeButton("取消", null).setPositiveButton("保存并重建") { _, _ -> runWork {
                    db.acceptRebuild(File(getExternalFilesDir(null), "cloud-recovery")); SyncCoordinator(this, session).sync(); "已保存副本并重建"
                } }.show()
        }
        button("设备列表与撤销") { runWorkUi {
            val list = GatewayClient(session.baseUrl, session).request("GET", "/v1/devices").getJSONArray("devices")
            val devices = (0 until list.length()).map { list.getJSONObject(it) }
            runOnUiThread { AlertDialog.Builder(this).setTitle("选择要撤销的设备")
                .setItems(devices.map { "${it.optString("name")} / ${it.optString("platform")}" }.toTypedArray()) { _, n ->
                    val target = devices[n]
                    AlertDialog.Builder(this).setMessage("撤销 ${target.optString("name")}？")
                        .setNegativeButton("取消", null).setPositiveButton("撤销") { _, _ -> runWork {
                            GatewayClient(session.baseUrl, session).request("DELETE", "/v1/devices/${target.getString("id")}")
                            if (target.getString("id") == session.deviceId) SessionStore(this).clear()
                            "设备已撤销"
                        } }.show()
                }.setNegativeButton("关闭", null).show() }
        } }
        button("退出云账号") { runWork {
            // Do not pretend a failed remote revocation succeeded. Local sign-out is offered explicitly.
            GatewayClient(session.baseUrl, session).request("POST", "/v1/auth/logout")
            SessionStore(this).clear(); "设备会话已撤销；各账号本地队列分别保留"
        } }
        button("仅清除本机登录状态（离线切换账号）") {
            SessionStore(this).clear(); render()
        }
    }
    private fun showConflicts(db: LocalDatabase) {
        val conflicts = db.conflicts()
        if (conflicts.isEmpty()) { showText("同步状态", "没有冲突。依赖失败的草稿可在“被拒草稿”中查看错误并明确重新排队。"); return }
        AlertDialog.Builder(this).setTitle("冲突 ${conflicts.size} 条")
            .setItems(conflicts.map { "${it.getString("entity_type")} / ${it.getString("uid")}" }.toTypedArray()) { _, n ->
                val c = conflicts[n]
                AlertDialog.Builder(this).setTitle("保留本地或云端")
                    .setMessage("本地：\n${c.getJSONObject("local").toString(2)}\n\n云端：\n${c.getJSONObject("remote").toString(2)}")
                    .setNegativeButton("取消", null).setNeutralButton("使用云端") { _, _ -> runWork { db.resolve(c.getString("op_id"), false); "已选择云端" } }
                    .setPositiveButton("保留本地并上传") { _, _ -> runWork { db.resolve(c.getString("op_id"), true); CloudRuntime.schedule(this); "本地版本已重新排队" } }.show()
            }.setNegativeButton("关闭", null).show()
    }
    private fun showRejected(db: LocalDatabase, session: CloudSession) {
        if (!CloudRuntime.current(this, session)) { Toast.makeText(this, "账号已变化，请重新打开云账号页", Toast.LENGTH_LONG).show(); return }
        val rejected = db.rejectedOperations()
        if (rejected.isEmpty()) { showText("被拒草稿", "没有被拒绝的同步操作。"); return }
        val dependencyCount = rejected.count { it.getString("code") == "dependency_failed" }
        AlertDialog.Builder(this).setTitle("被拒草稿 ${rejected.size} 条")
            .setItems(rejected.map { "${it.getString("entity_type")} · ${it.getString("code")} · ${it.getString("entity_uid")}" }.toTypedArray()) { _, index ->
                showText("同步草稿与错误", rejected[index].toString(2))
            }.setNegativeButton("关闭", null)
            .setPositiveButton("重排依赖失败草稿（$dependencyCount）") chooseRequeue@ { _, _ ->
                if (dependencyCount == 0) { showText("没有可重排的依赖草稿", "仅 dependency_failed 可手动重排；其他错误请先修正数据，原草稿仍可查看或导出。"); return@chooseRequeue }
                AlertDialog.Builder(this).setTitle("确认重新排队 $dependencyCount 条文字草稿")
                    .setMessage("请先解决父对象冲突。将先同步最新状态，再为依赖失败草稿生成新的操作编号，保留正文与先后顺序；已删除的对象或父对象不能复活。此操作不会创建或重试模型分析任务。")
                    .setNegativeButton("取消", null).setPositiveButton("确认重新排队") confirmRequeue@ { _, _ ->
                        val binding = try { CloudAccountBinding.capture(AndroidCloudSessionAccess(this)) }
                            catch (e: Exception) { Toast.makeText(this, e.message ?: "请先登录", Toast.LENGTH_LONG).show(); return@confirmRequeue }
                        runWork {
                            check(binding.session.accountKey == session.accountKey && binding.session.deviceId == session.deviceId) { "账号已变化" }
                            SyncCoordinator(this, binding.withCurrent { it }).sync()
                            val count = binding.withCurrent { db.requeueRejectedDependencies() }
                            CloudRuntime.schedule(this)
                            "已将 $count 条依赖草稿以新操作编号重新排队；未创建模型任务"
                        }
                    }.show()
            }.show()
    }
    private fun runWork(block: () -> String) {
        if (busy) return
        busy = true; status.text = "处理中…"
        worker.execute {
            val message = try { block() } catch (e: Exception) { e.message ?: "操作失败" }
            runOnUiThread { if (!isFinishing && !isDestroyed) { busy = false; render(); status.text = message } }
        }
    }
    private fun runWorkUi(block: () -> Unit) { runWork { block(); "" } }
    private fun showText(title: String, text: String) = AlertDialog.Builder(this).setTitle(title)
        .setView(ScrollView(this).apply { addView(TextView(this@CloudAccountActivity).apply { setPadding(20, 20, 20, 20); this.text = text; setTextIsSelectable(true) }) })
        .setPositiveButton("关闭", null).show()
    private fun label(text: String, size: Float = 14f) = TextView(this).apply { this.text = text; textSize = size; setPadding(0, 12, 0, 12); root.addView(this) }
    private fun field(value: String, password: Boolean = false) = EditText(this).apply {
        setText(value); inputType = if (password) InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD else InputType.TYPE_CLASS_TEXT
        root.addView(this)
    }
    private fun button(text: String, action: () -> Unit) { root.addView(Button(this).apply { this.text = text; setOnClickListener { if (!busy) action() } }) }
    override fun onDestroy() { super.onDestroy(); worker.shutdownNow() }
}
