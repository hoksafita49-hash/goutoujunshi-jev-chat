package com.jev.probe

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.widget.*
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import com.jev.probe.cloud.*
import com.jev.probe.data.LocalDatabase
import com.jev.probe.sync.SyncCoordinator
import org.json.JSONObject
import java.util.concurrent.Executors
import java.util.concurrent.CancellationException

/** Review screens use the account captured on entry; HTTP results never rebind to a new login. */
class CloudEvidenceMemoryActivity : AppCompatActivity() {
    private val worker = Executors.newSingleThreadExecutor()
    private lateinit var binding: CloudAccountBinding
    private lateinit var repo: EvidenceMemoryRepository
    private lateinit var root: LinearLayout
    private lateinit var status: TextView
    private val dialogs = mutableListOf<AlertDialog>()
    private var unobserve: (() -> Unit)? = null
    private var busy = false
    private var generation = 0

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        try {
            binding = CloudAccountBinding.capture(AndroidCloudSessionAccess(this))
            check(binding.session.accountKey == intent.getStringExtra("account") && binding.session.deviceId == intent.getStringExtra("device") &&
                CloudAccountBoundary.revision() == intent.getLongExtra("revision", -1)) { "账号已变化，请重新打开" }
            repo = EvidenceMemoryRepository(LocalDatabase.get(this, binding.session.accountKey), binding)
            unobserve = CloudAccountBoundary.observe { runOnUiThread { clearAndClose() } }
            val analysis = intent.getStringExtra("analysis")
            if (analysis == null) memories() else evidence(analysis)
        } catch (e: Exception) { Toast.makeText(this, e.message ?: "请重新登录", Toast.LENGTH_LONG).show(); finish() }
    }
    override fun onResume() {
        super.onResume()
        if (::binding.isInitialized) try { binding.assertCurrent() } catch (_: Exception) { clearAndClose() }
    }
    private fun clearAndClose() {
        generation++
        dialogs.toList().forEach { it.dismiss() }; dialogs.clear()
        if (::root.isInitialized) root.removeAllViews()
        finish()
    }
    private fun page(title: String) {
        binding.assertCurrent(); generation++
        root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(26, 24, 26, 40) }
        root.padForSystemBars()
        setContentView(ScrollView(this).apply { addView(root) })
        label(title, 23f)
        status = label("")
    }
    private fun evidence(uid: String) {
        page("分析原文证据")
        label("这里展示原分析时的消息快照，之后更正的聊天不会改写此快照。有原文引用只表示出处匹配，仍需人工判断说法。")
        val analysis = repo.db.record("analyses", uid)
        if (analysis == null || !analysis.isNull("deleted_at")) { label("分析不存在或已删除"); return }
        network({ repo.evidence(analysis) }, { evidence(uid) }) { view ->
            val result = analysis.getJSONObject("payload").optJSONObject("result") ?: analysis.getJSONObject("payload")
            result.optString("strategy").takeIf { it.isNotBlank() }?.let { label("策略：$it") }
            if (view.claims.isEmpty()) label("旧分析没有可回看的说法或原话引用，引用不可用。")
            for (claim in view.claims) {
                label("${kind(claim.kind)}：${claim.text}", 18f)
                if (claim.quotes.isEmpty()) label("原话引用不可用；此说法仍需人工核对。")
                else {
                    label("有原文引用")
                    for (quote in claim.quotes) {
                        label("“${quote.quote}”\n${speaker(quote.speaker)} · ${quote.observedAt.ifBlank { "时间未提供" }}\n消息版本 ${quote.version}")
                        action("回看此条完整原消息快照") {
                            dialog(AlertDialog.Builder(this).setTitle("原分析时的消息快照")
                                .setMessage("${speaker(quote.speaker)} · ${quote.observedAt}\n版本 ${quote.version}\n\n${quote.originalText}\n\n消息 ${quote.messageUid}")
                                .setPositiveButton("关闭", null))
                        }
                    }
                }
                action("将此说法保存为待确认记忆") {
                    dialog(AlertDialog.Builder(this).setTitle("保存为待确认记忆")
                        .setMessage("${claim.text}\n\n${if (claim.kind == "fact") "作为待确认事实保存" else "作为待确认假设保存"}，不会自动确认。${if (claim.quotes.isEmpty()) "没有可用原话，将标记为人工录入。" else "引用仍由服务器核对。"}")
                        .setNegativeButton("取消", null).setPositiveButton("保存待确认") { _, _ -> local {
                            repo.fromClaim(view.profileUid, claim); "已保存为待确认记忆，等待同步"
                        } })
                }
            }
            action("打开人物记忆") { memories() }
        }
    }
    private fun memories() {
        page("人物记忆")
        label("先保存为待确认，再由你单独确认事实。假设不能确认。离线修改进入原有上传队列；冲突请到云账号页处理。")
        action("立即同步并刷新记忆") { synchronize { memories() } }
        action("手动新增记忆") {
            val people = repo.db.records("profiles")
            if (people.isEmpty()) { status.text = "请先创建人物或同步云端人物"; return@action }
            dialog(AlertDialog.Builder(this).setTitle("选择人物")
                .setItems(people.map { it.getJSONObject("payload").optString("name", it.getString("uid")) }.toTypedArray()) { _, n ->
                    edit(null, people[n].getString("uid"))
                }.setNegativeButton("取消", null))
        }
        val items = repo.db.records("memories")
        if (items.isEmpty()) label("还没有记忆。可手动新增，或从分析证据保存说法。")
        for (record in items) {
            val payload = record.getJSONObject("payload")
            val person = repo.db.record("profiles", payload.getString("profile_uid"))?.getJSONObject("payload")?.optString("name") ?: "人物"
            action("$person · ${state(record)}\n${payload.optString("text")}") { memory(record.getString("uid")) }
        }
    }
    private fun state(record: JSONObject): String {
        val payload = record.getJSONObject("payload")
        val pending = repo.db.hasLocalChanges("memories", record.getString("uid"))
        return when {
            pending && payload.optString("status") == "confirmed" -> "事实 · 本地请求确认，尚未同步"
            pending -> "${kind(payload.optString("kind"))} · 本地待上传（${if (payload.optString("status") == "confirmed") "请求确认" else "待确认"}）"
            payload.optString("status") == "confirmed" -> "事实 · 云端已确认"
            else -> "${kind(payload.optString("kind"))} · 云端待确认"
        }
    }
    private fun memory(uid: String) {
        page("记忆详情")
        val record = repo.db.record("memories", uid)
        if (record == null || !record.isNull("deleted_at")) { label("记忆已删除或不存在"); return }
        val p = record.getJSONObject("payload")
        label(state(record), 18f); label(p.getString("text"), 20f)
        label("当前云版本：${record.getInt("version")}\n修改原因：${p.optString("change_reason").ifBlank { "未填写" }}\n来源：${if (p.optString("source_type") == "messages") "消息" else "人工录入"}")
        val sources = CloudProtocol.strings(p, "source_uids")
        if (sources.isNotEmpty()) label("来源消息：\n${sources.joinToString("\n")}")
        action("更正正文 / 改为事实或假设") { edit(record, p.getString("profile_uid")) }
        if (p.optString("kind") == "fact" && p.optString("status") != "confirmed" && !repo.db.hasLocalChanges("memories", uid)) action("单独确认此事实") {
            dialog(AlertDialog.Builder(this).setTitle("确认事实")
                .setMessage("请核对正文与来源后确认：\n${p.getString("text")}\n\n确认请求需同步成功后，才显示为云端已确认。")
                .setNegativeButton("取消", null).setPositiveButton("确认事实") { _, _ -> local({ memory(uid) }) {
                    repo.update(record, p.getString("text"), "fact", "confirmed", "手动确认事实"); "确认请求已进入上传队列"
                } })
        }
        if (p.optString("status") != "confirmed" && repo.db.hasLocalChanges("memories", uid)) label("请先同步新增或更正，再单独确认事实。")
        if (p.optString("kind") == "hypothesis") label("假设不能确认。若你有充分依据，请先编辑并明确改为事实、保存后再单独确认。")
        if (p.optString("status") == "confirmed") action("退回待确认") { local({ memory(uid) }) {
            repo.update(record, p.getString("text"), p.getString("kind"), "pending", "手动退回待确认"); "已退回待确认，等待同步"
        } }
        action("查看云端历史（需要网络）") { history(uid) }
        action("立即同步并刷新当前记忆") { synchronize { memory(uid) } }
        action("删除记忆") {
            dialog(AlertDialog.Builder(this).setTitle("确认删除记忆")
                .setMessage("${p.getString("text")}\n\n同步后，已删除记忆不能通过历史恢复。")
                .setNegativeButton("取消", null).setPositiveButton("删除") { _, _ -> local({ memories() }) {
                    repo.delete(record, true); "删除已进入上传队列"
                } })
        }
        action("返回记忆列表") { memories() }
    }
    private fun edit(record: JSONObject?, profile: String) {
        val box = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(24, 10, 24, 10) }
        fun field(hint: String, value: String) = EditText(this).apply { this.hint = hint; setText(value); box.addView(this) }
        val p = record?.getJSONObject("payload")
        val text = field("记忆正文（最多 4000 字）", p?.optString("text").orEmpty())
        val select = Spinner(this).apply {
            adapter = ArrayAdapter(this@CloudEvidenceMemoryActivity, android.R.layout.simple_spinner_dropdown_item, listOf("事实（先待确认）", "假设（不能确认）"))
            setSelection(if (p?.optString("kind") == "hypothesis") 1 else 0); box.addView(this)
        }
        val reason = field("修改原因（最多 500 字）", if (record == null) "手动录入" else "")
        box.addView(TextView(this).apply { this.text = "保存后回到待确认。原消息来源及服务端字段会保留。" })
        val dialog = AlertDialog.Builder(this).setTitle(if (record == null) "新增待确认记忆" else "更正记忆")
            .setView(ScrollView(this).apply { addView(box) }).setNegativeButton("取消", null).setPositiveButton("保存待确认", null).create()
        dialogs.add(dialog); dialog.setOnDismissListener { dialogs.remove(dialog) }; dialog.show()
        dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
            val successful = local({ memories() }) {
                val chosenKind = if (select.selectedItemPosition == 0) "fact" else "hypothesis"
                if (record == null) repo.create(profile, text.text.toString(), chosenKind, reason.text.toString())
                else repo.update(record, text.text.toString(), chosenKind, "pending", reason.text.toString())
                "记忆已保存为待确认，等待同步"
            }
            if (successful) dialog.dismiss()
        }
    }
    private fun history(uid: String, before: Int = 0) {
        page("记忆历史")
        label("历史需要网络。恢复会以当前可见版本为基础新增待确认修改，不会倒退同步版本；服务器会重新核对来源。")
        action("返回当前记忆") { memory(uid) }
        network({ repo.history(uid, before) }, { history(uid, before) }) { page ->
            if (page.versions.isEmpty()) label("尚无云端历史；本地草稿需要先同步。")
            for (version in page.versions) {
                val p = version.getJSONObject("payload")
                action("版本 ${version.getInt("version")} · ${kind(p.optString("kind"))} · ${if (p.optString("status") == "confirmed") "当时已确认" else "当时待确认"}\n${p.getString("text")}\n${version.optString("updated_at")}\n原因：${p.optString("change_reason")}") {
                    dialog(AlertDialog.Builder(this).setTitle("恢复版本 ${version.getInt("version")} 为新待确认修改")
                        .setMessage("${p.getString("text")}\n\n${CloudProtocol.strings(p, "source_uids").joinToString("\n")}\n\n以当前版本提交，状态为待确认。若有并发修改，将保留原有冲突处理。")
                        .setNegativeButton("取消", null).setPositiveButton("恢复为待确认") { _, _ -> local({ memory(uid) }) {
                            repo.restore(checkNotNull(repo.db.record("memories", uid)), version); "恢复内容已进入待确认上传队列"
                        } })
                }
            }
            if (page.hasMore) action("更早版本") { history(uid, page.nextBeforeVersion) }
            if (before > 0) action("返回最新历史") { history(uid) }
        }
    }
    private fun local(after: () -> Unit = {}, block: () -> String): Boolean = try {
        val message = binding.withCurrent { block() }
        CloudRuntime.schedule(this); after(); status.text = message; true
    } catch (e: Exception) {
        val message = e.message ?: "操作失败"; status.text = message
        Toast.makeText(this, message, Toast.LENGTH_LONG).show(); false
    }
    private fun synchronize(after: () -> Unit) {
        network({ SyncCoordinator(this, binding.withCurrent { it }).sync() }, { synchronize(after) }) { after() }
    }
    private fun <T> network(read: () -> T, retry: () -> Unit, display: (T) -> Unit) {
        if (busy) return
        busy = true; status.text = "正在读取云端…"
        val viewGeneration = generation
        worker.execute {
            val response = runCatching { binding.assertCurrent(); read() }
            runOnUiThread {
                if (isFinishing || isDestroyed || viewGeneration != generation) return@runOnUiThread
                busy = false
                try {
                    binding.withCurrent {
                        response.fold({ status.text = ""; display(it) }, { error ->
                            if (error is CloudException && error.status == 401) { SessionStore(this).clear(); return@fold }
                            status.text = if (error is CloudException && error.status == 404) "资料已删除或不存在，无法查看。" else error.message ?: "读取失败，请联网后重试"
                            action("重新读取", retry)
                        })
                    }
                } catch (_: Exception) { clearAndClose() }
            }
        }
    }
    private fun dialog(builder: AlertDialog.Builder) {
        binding.assertCurrent()
        val shown = builder.create(); dialogs.add(shown); shown.setOnDismissListener { dialogs.remove(shown) }; shown.show()
    }
    private fun label(text: String, size: Float = 15f) = TextView(this).apply {
        this.text = text; textSize = size; setTextIsSelectable(true); setPadding(0, 10, 0, 10); root.addView(this)
    }
    private fun action(text: String, block: () -> Unit) {
        root.addView(Button(this).apply {
            this.text = text
            setOnClickListener {
                if (!busy) try { binding.withCurrent { block() } }
                catch (_: CancellationException) { clearAndClose() }
                catch (e: Exception) { Toast.makeText(this@CloudEvidenceMemoryActivity, e.message ?: "操作失败", Toast.LENGTH_LONG).show() }
            }
        })
    }
    private fun kind(value: String) = when (value) { "fact" -> "事实说法"; "hypothesis" -> "假设"; else -> "未知" }
    private fun speaker(value: String) = when (value) { "me" -> "我"; "other" -> "对方"; else -> "说话人未知" }
    override fun onDestroy() { generation++; unobserve?.invoke(); dialogs.toList().forEach { it.dismiss() }; worker.shutdownNow(); super.onDestroy() }
    companion object {
        fun open(context: Context, session: CloudSession, analysis: String? = null) {
            context.startActivity(Intent(context, CloudEvidenceMemoryActivity::class.java).putExtra("account", session.accountKey)
                .putExtra("device", session.deviceId).putExtra("revision", CloudAccountBoundary.revision()).putExtra("analysis", analysis))
        }
    }
}
