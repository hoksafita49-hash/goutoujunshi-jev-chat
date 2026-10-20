"""Foreground cloud account, explicit identity binding, migration and cached data UI."""
import json
import threading
import time
import uuid
from PySide6.QtCore import QObject, QTimer, Signal, Qt
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QComboBox, QDialog, QPlainTextEdit, QMessageBox)
from . import settings
from .cloud_runtime import current
from .gateway import validate_server
from .legacy_import import preview_profiles, import_profiles


class _Signals(QObject):
    done = Signal(object, object, bool)


class CloudPanel(QWidget):
    def __init__(self, overlay):
        super().__init__()
        self.overlay, self.busy, self.failures, self.next_sync = overlay, False, 0, 0
        self.account_dialogs = []
        self._account_key = None
        self._run_context = None
        self._completion = None
        self._failure = None
        self.signals = _Signals(self)
        self.signals.done.connect(self._finished)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 18)
        layout.addWidget(QLabel("云账号与三端同步"))
        self.mode = QComboBox()
        self.mode.addItems(["本机模型直连（手动配置 Key）", "统一云服务（账号登录）"])
        self.mode.setCurrentIndex(1 if settings.cloud_mode() else 0)
        layout.addWidget(self.mode)
        self.server = QLineEdit(settings.cloud_server())
        self.server.setPlaceholderText("https://api.zovoeh.cn")
        self.server.setAccessibleName("云服务器地址")
        layout.addWidget(self.server)
        self.apply = QPushButton("应用连接方式和服务器地址")
        self.apply.clicked.connect(self.apply_mode)
        layout.addWidget(self.apply)
        self.email, self.password = QLineEdit(), QLineEdit()
        self.email.setPlaceholderText("账号邮箱（由服务端创建）")
        self.password.setPlaceholderText("账号密码")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.email.setAccessibleName("云账号邮箱")
        self.password.setAccessibleName("云账号密码")
        layout.addWidget(self.email)
        layout.addWidget(self.password)
        self.status = QLabel("云模式只使用设备本地 OCR；模型 Key 由服务端管理。")
        self.status.setWordWrap(True)
        buttons = QHBoxLayout()
        for title, handler in (("登录", self.login), ("退出此设备", self.logout), ("立即同步", self.sync)):
            button = QPushButton(title)
            button.clicked.connect(handler)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        layout.addWidget(self.status)
        self.people = QComboBox()
        self.people.setAccessibleName("云端人物列表")
        layout.addWidget(self.people)
        self.name = QLineEdit()
        self.name.setPlaceholderText("新人物名称（同名不会自动合并）")
        layout.addWidget(self.name)
        row = QHBoxLayout()
        for title, handler in (("新建人物", self.create_profile), ("关联当前会话", self.bind), ("查看资料", self.browse)):
            button = QPushButton(title)
            button.clicked.connect(handler)
            row.addWidget(button)
        layout.addLayout(row)
        edit_profile = QPushButton("编辑所选人物资料")
        edit_profile.clicked.connect(self.edit_profile)
        layout.addWidget(edit_profile)
        self.memory_button = QPushButton("长期记忆 · 新增、更正、确认与历史")
        self.memory_button.clicked.connect(self.memories)
        layout.addWidget(self.memory_button)
        self.recovery = QPushButton("服务器恢复后：保留本地副本并重新拉取")
        self.recovery.clicked.connect(self.recover)
        self.recovery.hide()
        layout.addWidget(self.recovery)
        row = QHBoxLayout()
        for title, handler in (("旧人物迁移预览", self.migrate), ("处理冲突", self.conflicts), ("通用笔记", self.notes)):
            button = QPushButton(title)
            button.clicked.connect(handler)
            row.addWidget(button)
        layout.addLayout(row)
        hint = QLabel("关联需由你确认；旧人物不会按昵称与其他端自动合并。账号退出后隐藏本机账号资料。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.timer = QTimer(self)
        self.timer.setInterval(15000)
        self.timer.timeout.connect(self._poll)
        self.timer.start()
        self.refresh()

    def apply_mode(self):
        if self.busy:
            self.status.setText("当前连接操作完成后再切换服务器或模式")
            return
        try:
            server = validate_server(self.server.text())
            self._close_account_dialogs()
            settings.save(cloud_mode_on=self.mode.currentIndex() == 1, cloud_server_text=server)
            self.overlay.invalidate_replies()
            self.overlay.reset_chat_state()
            if self.overlay.on_settings_saved:
                self.overlay.on_settings_saved()
            self.overlay._load_settings()
            self.refresh()
        except Exception as exc:
            self.status.setText(str(exc)[:240])

    def _run(self, fn, *, account_change=False, on_success=None, on_error=None):
        if self.busy:
            return
        self.busy = True
        service = current()
        self._run_context = (service, service.identity)
        if account_change:
            self._close_account_dialogs()
            self.overlay.invalidate_replies()
            self.overlay.reset_chat_state()
            self.overlay.settingsPage.setEnabled(False)
        self._completion = on_success
        self._failure = on_error
        self.status.setText("正在连接云服务…")
        def run():
            try:
                value, error = fn(), None
            except Exception as exc:
                value, error = None, str(exc)[:240]
            self.signals.done.emit(value, error, account_change)
        threading.Thread(target=run, daemon=True).start()

    def _finished(self, value, error, account_change):
        self.busy = False
        self.overlay.settingsPage.setEnabled(True)
        self.failures = min(6, self.failures + 1) if error else 0
        self.next_sync = time.monotonic() + (15 * 2 ** self.failures)
        stable = bool(self._run_context and settings.cloud_mode() and current() is self._run_context[0] and
                      current().identity == self._run_context[1])
        if account_change or not stable or (settings.cloud_mode() and not current().session):
            self._close_account_dialogs()
            self.overlay.reset_chat_state()
            self.overlay.invalidate_replies()
            if self.overlay.on_settings_saved:
                self.overlay.on_settings_saved()
        self.refresh()
        if not account_change and not stable:
            self._completion = self._failure = self._run_context = None
            return
        if not error:
            self.overlay._refresh_cloud_style(discard_draft=account_change)
        if isinstance(value, dict) and value.get("model_ready") is False:
            self.status.setText(self.status.text() + " · 服务端模型尚未配置，资料同步仍可用")
        if error:
            self.status.setText(error + "；本地队列保留，可修复连接后重试。")
            if self._failure:
                self._failure(error)
        elif self._completion:
            self._completion(value)
        self._completion = None
        self._failure = None
        self._run_context = None

    def _close_account_dialogs(self):
        for dialog in list(self.account_dialogs):
            dialog.reject()
        self.account_dialogs.clear()

    def login(self):
        if not settings.cloud_mode():
            self.status.setText("请先应用“统一云服务”连接方式")
            return
        email, password = self.email.text(), self.password.text()
        self.password.clear()
        if not email.strip() or not password:
            self.status.setText("请填写邮箱和密码")
            return
        service = current()
        self._run(lambda: service.login(email, password), account_change=True)

    def logout(self):
        self._run(current().logout, account_change=True)

    def sync(self):
        if settings.cloud_mode():
            self._run(current().sync)

    def _poll(self):
        if (settings.cloud_mode() and current().session and not current().require().db.frozen and not self.busy and
                QApplication.applicationState() == Qt.ApplicationState.ApplicationActive and
                time.monotonic() >= self.next_sync):
            self.sync()

    def refresh(self):
        service = current() if settings.cloud_mode() else None
        account_key = (service, service.identity) if service else None
        if account_key != self._account_key:
            self._close_account_dialogs()
            self._account_key = account_key
        self.people.clear()
        self.profiles = []
        self.recovery.hide()
        if not settings.cloud_mode():
            self.status.setText("本机直连模式；云账号与同步未启用。")
            return
        service = current()
        if not service.session:
            self.status.setText("尚未登录；仅保存服务器地址，不保存密码。")
            return
        db = service.require().db
        self.recovery.setVisible(db.frozen)
        self.profiles = db.list_records("profiles")
        for row in self.profiles:
            self.people.addItem(f"{row['payload'].get('name', '未命名')} · {row['uid'][:8]}")
        counts = db.status_counts()
        self.status.setText(f"{service.session['user']['email']} · 待上传 {sum(counts.values())} · "
                            f"冲突 {len(db.conflicts())} · 被拒 {counts.get('rejected', 0)}" +
                            (" · 服务器恢复副本待处理，上传冻结" if db.frozen else " · 前台每15秒检查同步"))

    def _repo(self):
        if not settings.cloud_mode():
            raise ValueError("请先启用统一云服务")
        return current().require()

    def _selected(self):
        index = self.people.currentIndex()
        if index < 0 or index >= len(self.profiles):
            raise ValueError("请先创建或选择云端人物")
        return self.profiles[index]

    def memories(self):
        try:
            from .cloud_memory_ui import MemoryDialog
            MemoryDialog(self, self._selected()["uid"]).open()
        except Exception as exc:
            self.status.setText(str(exc))

    def show_evidence(self, result):
        try:
            from .cloud_memory_ui import EvidenceDialog
            service = current()
            self._repo()
            if result.get("cloud_identity", service.identity) != service.identity:
                raise RuntimeError("account_changed")
            EvidenceDialog(self, result).open()
        except Exception as exc:
            self.status.setText(str(exc))

    def create_profile(self):
        try:
            name = self.name.text().strip()
            if not name or len(name) > 200:
                raise ValueError("人物名称需要1至200字")
            self._repo().db.save("profiles", str(uuid.uuid4()), dict(name=name, stage="未填写", goal="自然接话", background="", notes=""))
            self.name.clear()
            self.refresh()
        except Exception as exc:
            self.status.setText(str(exc))

    def bind(self):
        try:
            profile = self._selected()
            title = self.overlay.current_chat()
            if not title:
                raise ValueError("请先打开聊天并开启本地采集，识别当前会话")
            answer = QMessageBox.question(self, "确认人物关联", f"把当前 {settings.target_app()} 会话「{title}」关联到\n"
                                          f"{profile['payload']['name']}\n{profile['uid']}？")
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._repo().bind_chat(settings.target_app(), title, profile["uid"], current().source_device)
            self.status.setText("关联已保存在本机待上传队列；核对聊天后即可云端分析。")
            self.overlay._load_settings()
        except Exception as exc:
            self.status.setText(str(exc))

    def edit_profile(self):
        try:
            db = self._repo().db
            row = self._selected()
            dialog = QDialog(self)
            self.account_dialogs.append(dialog)
            dialog.setWindowTitle("人物资料 · " + row["uid"][:8])
            dialog.resize(480, 580)
            layout = QVBoxLayout(dialog)
            payload = row["payload"]
            name, stage, goal = QLineEdit(payload.get("name", "")), QComboBox(), QComboBox()
            stage.addItems(list(settings.STAGES))
            stage.setCurrentText(payload.get("stage", "未填写"))
            goal.addItems(list(settings.GOALS))
            goal.setCurrentText(payload.get("goal", "自然接话"))
            background, notes = QPlainTextEdit(payload.get("background", "")), QPlainTextEdit(payload.get("notes", ""))
            for text, widget in (("人物称呼", name), ("关系阶段", stage), ("本轮目标", goal), ("已知背景", background), ("个人备注", notes)):
                layout.addWidget(QLabel(text))
                layout.addWidget(widget)
            save = QPushButton("保存到本机并等待同步")
            error = QLabel("")
            def persist():
                if not name.text().strip():
                    error.setText("请填写人物称呼")
                    return
                try:
                    live = db.visible("profiles", row["uid"])
                    expected = live["version"] if live and live["payload"] == payload else row["version"]
                    db.save("profiles", row["uid"], {**payload, "name": name.text().strip(), "stage": stage.currentText(),
                            "goal": goal.currentText(), "background": background.toPlainText(), "notes": notes.toPlainText()},
                            expected_version=expected)
                    dialog.accept()
                except Exception as exc:
                    error.setText(str(exc))
            save.clicked.connect(persist)
            layout.addWidget(error)
            layout.addWidget(save)
            dialog.exec()
            self.refresh()
        except Exception as exc:
            self.status.setText(str(exc))

    def migrate(self):
        try:
            repo = self._repo()
            rows = preview_profiles(settings._CONFIG)
            listing = "\n".join(f"{r['app']} · {r['title']} · {r['stage']} · {r['goal']}" for r in rows)
            answer = QMessageBox.question(self, "旧人物迁移预览", f"将导入 {len(rows)} 条人物档案：\n{listing}\n\n"
                                          "不同应用的同名人物独立保留；重复导入跳过已存在项。源文件保留。确认导入当前账号？")
            if answer == QMessageBox.StandardButton.Yes:
                device = current().source_device
                self._run(lambda: import_profiles(repo.db, rows, device), on_success=lambda count:
                          self.status.setText(f"已导入 {count} 条；旧文件保留，队列等待同步。"))
        except Exception as exc:
            self.status.setText(str(exc))

    def recover(self):
        try:
            service = current()
            db = service.require().db
            answer = QMessageBox.question(self, "确认服务器恢复处理", "已保存恢复前的完整 SQLite 副本（含旧待上传队列）：\n"
                + str(db.path.parent) + "\n\n确认从恢复后的服务器重新拉取？旧队列只保留在恢复副本，不能自动上传；"
                "需人工核对差异后再重新录入。")
            if answer == QMessageBox.StandardButton.Yes:
                self._run(service.recover_after_review, account_change=True)
        except Exception as exc:
            self.status.setText(str(exc))

    def conflicts(self):
        try:
            db = self._repo().db
            for conflict in db.conflicts():
                dialog = QMessageBox(self)
                dialog.setWindowTitle("选择要保留的同步版本")
                dialog.setText(f"{conflict['kind']} · {conflict['uid']}\n本地与远端内容均已保留。")
                dialog.setDetailedText("本地：\n" + json.dumps(conflict["local"], ensure_ascii=False, indent=2) +
                                       "\n远端：\n" + json.dumps(conflict["remote"], ensure_ascii=False, indent=2))
                local = dialog.addButton("采用本地，重新上传", QMessageBox.ButtonRole.AcceptRole)
                remote = dialog.addButton("采用远端版本", QMessageBox.ButtonRole.DestructiveRole)
                dialog.addButton("暂不处理", QMessageBox.ButtonRole.RejectRole)
                local.setEnabled(not bool(conflict["remote"].get("deleted_at")))
                dialog.exec()
                if dialog.clickedButton() is local:
                    db.resolve(conflict["id"], "local")
                elif dialog.clickedButton() is remote:
                    db.resolve(conflict["id"], "remote")
            rejected = db.status_counts().get("rejected", 0)
            if rejected and QMessageBox.question(self, "被服务器拒绝的草稿", f"仍保留 {rejected} 条被拒操作。"
                "请先修复相关人物、会话或内容。是否为这些草稿创建新操作 ID 并重新排队？"
                "这只重试资料同步，不创建付费分析任务。") == QMessageBox.StandardButton.Yes:
                db.retry_rejected()
            self.refresh()
        except Exception as exc:
            self.status.setText(str(exc))

    def notes(self):
        try:
            db = self._repo().db
            dialog = QDialog(self)
            self.account_dialogs.append(dialog)
            dialog.setWindowTitle("通用笔记 · 不作为人物事实")
            dialog.resize(520, 520)
            layout = QVBoxLayout(dialog)
            chooser, title, body = QComboBox(), QLineEdit(), QPlainTextEdit()
            rows = db.list_records("notes")
            chooser.addItem("新建笔记")
            for row in rows:
                chooser.addItem(row["payload"].get("title", "未命名"))
            def load(index):
                payload = rows[index-1]["payload"] if index else {}
                title.setText(payload.get("title", ""))
                body.setPlainText(payload.get("body", ""))
            chooser.currentIndexChanged.connect(load)
            for widget in (chooser, title, body):
                layout.addWidget(widget)
            save = QPushButton("保存到当前账号待同步队列")
            def persist():
                index = chooser.currentIndex()
                uid = rows[index-1]["uid"] if index else str(uuid.uuid4())
                old = rows[index-1]["payload"] if index else dict(tags=[], enabled=True)
                live = db.visible("notes", uid) if index else None
                expected = (live["version"] if live and live["payload"] == old else rows[index-1]["version"]) if index else None
                db.save("notes", uid, {**old, "title": title.text().strip() or "笔记", "body": body.toPlainText()},
                        expected_version=expected)
                dialog.accept()
            save.clicked.connect(persist)
            layout.addWidget(save)
            dialog.exec()
            self.refresh()
        except Exception as exc:
            self.status.setText(str(exc))

    def browse(self):
        try:
            repo = self._repo()
            profile = self._selected()
            dialog = QDialog(self)
            self.account_dialogs.append(dialog)
            dialog.setWindowTitle("已同步资料 · " + profile["payload"].get("name", ""))
            dialog.resize(640, 600)
            layout = QVBoxLayout(dialog)
            chooser = QComboBox()
            view = QPlainTextEdit()
            view.setReadOnly(True)
            conversations = [r for r in repo.db.list_records("conversations") if r["payload"].get("profile_uid") == profile["uid"]]
            for conv in conversations:
                chooser.addItem(f"{conv['payload'].get('platform', '')} · {conv['payload'].get('title', '')} · {conv['uid'][:8]}")
            layout.addWidget(chooser)
            layout.addWidget(view)
            candidates = QComboBox()
            layout.addWidget(candidates)
            evidence_chooser = QComboBox()
            evidence_chooser.setAccessibleName("选择要核对证据的历史分析")
            layout.addWidget(evidence_chooser)
            evidence_button = QPushButton("查看所选分析的证据与原文出处")
            evidence_button.clicked.connect(lambda: self.show_evidence(evidence_chooser.currentData()) if evidence_chooser.currentData() else None)
            layout.addWidget(evidence_button)
            def render():
                p = repo.db.visible("profiles", profile["uid"])
                if not p or p.get("deleted_at"):
                    view.setPlainText("此人物已删除")
                    candidates.clear()
                    return
                payload = p["payload"]
                lines = [payload.get("name", ""), "阶段：" + payload.get("stage", ""),
                         "目标：" + payload.get("goal", ""), "背景：" + payload.get("background", ""),
                         "备注：" + payload.get("notes", "")]
                candidates.clear()
                evidence_chooser.clear()
                index = chooser.currentIndex()
                if index >= 0:
                    uid = conversations[index]["uid"]
                    messages = [r for r in repo.db.list_records("messages") if r["payload"].get("conversation_uid") == uid]
                    messages.sort(key=lambda r: (r["payload"].get("order_key", "~"), r["payload"].get("observed_at", ""), r["uid"]))
                    lines.append("\n已确认聊天：")
                    lines.extend(("我" if r["payload"].get("role") == "me" else "对方") + "：" + r["payload"].get("text", "") for r in messages)
                    analyses = [r for r in repo.db.list_records("analyses") if r["payload"].get("conversation_uid") == uid]
                    analyses.sort(key=lambda r: r.get("updated_at", ""), reverse=True)
                    for row in analyses:
                        result = row["payload"].get("result") or row["payload"]
                        if not result.get("analysis_id"):
                            continue
                        evidence_chooser.addItem(result["analysis_id"][:12] + " · 上下文版本 " + str(result.get("context_revision", "")),
                            {**result, "profile_uid": profile["uid"], "conversation_uid": uid, "cloud_identity": current().identity})
                        lines.extend(["\n分析 " + result["analysis_id"], "策略：" + result.get("strategy", ""),
                                      "事实：" + "；".join(result.get("facts", [])),
                                      "推测：" + "；".join(result.get("hypotheses", [])),
                                      "未知：" + "；".join(result.get("unknowns", [])),
                                      "停止条件：" + result.get("stop_condition", "")])
                        for number, candidate in enumerate(result.get("candidates", []), 1):
                            text = candidate["text"]
                            weight = candidate.get("weight") if result.get("ranking_status") == "available" else None
                            lines.append(f"{number}. {text}" + (f"（相对权重 {weight:.1%}）" if weight is not None else ""))
                            candidates.addItem(text, text)
                        if result.get("text"):
                            lines.append(result["text"])
                memories = [r for r in repo.db.list_records("memories") if r["payload"].get("profile_uid") == profile["uid"]]
                lines.append("\n长期记忆：")
                lines.extend(f"[{r['payload'].get('kind')} / {r['payload'].get('status')}] {r['payload'].get('text', '')}" for r in memories)
                view.setPlainText("\n".join(lines))
            chooser.currentIndexChanged.connect(render)
            render()
            copy = QPushButton("复制所选候选（由你手动发送）")
            copy.clicked.connect(lambda: QApplication.clipboard().setText(candidates.currentData() or ""))
            layout.addWidget(copy)
            buttons = QHBoxLayout()
            job_status = QLabel("")
            job_status.setWordWrap(True)
            layout.addWidget(job_status)
            resume = QPushButton("分析或继续查询此已确认会话")
            fresh = QPushButton("明确新建一次分析")
            def generate(new_request=False):
                index = chooser.currentIndex()
                if index < 0:
                    return
                if new_request and QMessageBox.question(dialog, "确认新的分析调用", "将创建新的请求，可能额外计费。"
                    "结果不明的旧任务仍保留；确认继续？") != QMessageBox.StandardButton.Yes:
                    return
                conv_uid = conversations[index]["uid"]
                service = current()
                def action():
                    def guarded():
                        service.sync()
                        conv = repo.db.visible("conversations", conv_uid)
                        if not conv or conv.get("deleted_at") or repo.db.has_pending_context(profile["uid"], conv_uid):
                            raise RuntimeError("当前会话已删除或仍有未同步内容")
                        result = repo.run_job(conv_uid, conv["payload"]["context_revision"], new_request=new_request)
                        service.sync()
                        return result
                    return service._guard(guarded)
                job_status.setText("正在查询/处理服务端任务，关闭此窗口后任务 ID 仍保留。")
                def completed(_result):
                    render()
                    job_status.setText("任务已完成；候选顺序与 ID 来自服务端。")
                self._run(action, on_success=completed, on_error=job_status.setText)
            resume.clicked.connect(lambda: generate(False))
            fresh.clicked.connect(lambda: generate(True))
            buttons.addWidget(resume)
            buttons.addWidget(fresh)
            layout.addLayout(buttons)
            layout.addWidget(QLabel("此页只显示当前账号的持久缓存；未联网时也可查看已同步资料。"))
            dialog.exec()
        except Exception as exc:
            self.status.setText(str(exc))
