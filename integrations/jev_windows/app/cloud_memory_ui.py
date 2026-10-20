"""Account-bound evidence and memory review dialogs. Network reads never block Qt."""
import threading
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
                              QPlainTextEdit, QLineEdit, QPushButton, QMessageBox)
from . import settings
from .cloud_runtime import current
from .gateway import CloudError, evidence_for_ui


_KINDS = {"fact": "事实", "hypothesis": "推测", "unknown": "未知"}


class _ReadSignals(QObject):
    done = Signal(object, object)


class _AccountDialog(QDialog):
    def __init__(self, panel, profile_uid):
        super().__init__(panel)
        self.panel, self.profile_uid = panel, profile_uid
        self.service, self.identity = current(), current().identity
        self.repo = self.service.require()
        self.closed, self.busy, self._completed = False, False, None
        self.layout = QVBoxLayout(self)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.signals = _ReadSignals(self)
        self.signals.done.connect(self._read_finished)
        panel.account_dialogs.append(self)
        self.finished.connect(self._clear_private)
        self.lifetime = QTimer(self)
        self.lifetime.setInterval(250)
        self.lifetime.timeout.connect(self._check_lifetime)
        self.lifetime.start()
        self.resize(640, 620)

    def _active(self):
        active = (not self.closed and settings.cloud_mode() and current() is self.service and
                  self.service.identity == self.identity and self.service.session and
                  self.service.repository is self.repo and not self.repo.db.frozen)
        profile = self.repo.db.visible("profiles", self.profile_uid) if active else None
        return bool(active and profile and not profile.get("deleted_at"))

    def _require_active(self):
        if not self._active():
            self.reject()
            raise RuntimeError("account_changed：账号或人物已变化，请重新打开")

    def _check_lifetime(self):
        if not self._active():
            self.reject()

    def _clear_private(self, _result=0):
        self.closed = True
        self._completed = None
        self.lifetime.stop()
        for widget in self.findChildren(QPlainTextEdit):
            widget.clear()
        for widget in self.findChildren(QLineEdit):
            widget.clear()
        for widget in self.findChildren(QComboBox):
            widget.blockSignals(True)
            widget.clear()
        self.status.clear()
        self.clear_data()
        if self in self.panel.account_dialogs:
            self.panel.account_dialogs.remove(self)

    def clear_data(self):
        pass

    def _read(self, action, completed):
        self._require_active()
        if self.busy:
            return
        self.busy, self._completed = True, completed
        self.status.setText("正在读取云端资料…")
        def run():
            try:
                value, error = action(), None
            except Exception as exc:
                value, error = None, exc
            self.signals.done.emit(value, error)
        threading.Thread(target=run, daemon=True).start()

    def _read_finished(self, value, error):
        self.busy = False
        completed, self._completed = self._completed, None
        if not self._active():
            self.reject()
            return
        if error:
            self.status.setText(str(error) + "；此视图需要网络，可重新加载。")
            self.read_failed(error)
        elif completed:
            try:
                completed(value)
            except Exception as exc:
                self.status.setText(str(exc))

    def read_failed(self, error):
        if isinstance(error, CloudError) and error.status == 404:
            self.reject()


class MemoryDialog(_AccountDialog):
    def __init__(self, panel, profile_uid):
        super().__init__(panel, profile_uid)
        self.setWindowTitle("长期记忆 · 由你核对与确认")
        self.rows, self.row = [], None
        self.chooser, self.kind = QComboBox(), QComboBox()
        self.kind.addItem("事实", "fact")
        self.kind.addItem("推测（不能直接确认）", "hypothesis")
        self.text, self.reason = QPlainTextEdit(), QLineEdit()
        self.text.setAccessibleName("记忆内容")
        self.reason.setAccessibleName("修改原因")
        self.reason.setMaxLength(500)
        self.reason.setPlaceholderText("填写本次新增、更正或状态变化的原因")
        self.sources = QLabel("")
        self.sources.setWordWrap(True)
        hint = QLabel("新增与更正需先同步，再单独确认事实。可关闭此窗口后点“立即同步”，再打开记忆确认。推测需先转为事实。")
        hint.setWordWrap(True)
        self.layout.addWidget(hint)
        for widget in (self.chooser, self.kind, self.text, QLabel("修改原因"), self.reason, self.sources, self.status):
            self.layout.addWidget(widget)
        self.save_button, self.confirm_button = QPushButton("保存为待确认"), QPushButton("明确确认事实")
        self.pending_button, self.delete_button = QPushButton("退回待确认"), QPushButton("删除记忆…")
        self.history_button = QPushButton("查看历史 / 恢复旧版本")
        row = QHBoxLayout()
        for button in (self.save_button, self.confirm_button, self.pending_button, self.delete_button):
            row.addWidget(button)
        self.layout.addLayout(row)
        self.layout.addWidget(self.history_button)
        self.chooser.currentIndexChanged.connect(self.load)
        self.text.textChanged.connect(self._confirmation_state)
        self.kind.currentIndexChanged.connect(self._confirmation_state)
        self.save_button.clicked.connect(self.save)
        self.confirm_button.clicked.connect(self.confirm)
        self.pending_button.clicked.connect(self.pending)
        self.delete_button.clicked.connect(self.delete)
        self.history_button.clicked.connect(self.history)
        self.refresh()

    def clear_data(self):
        self.rows, self.row = [], None
        self.sources.clear()

    def refresh(self, selected=None):
        self._require_active()
        self.rows = [r for r in self.repo.db.list_records("memories") if r["payload"].get("profile_uid") == self.profile_uid]
        self.chooser.blockSignals(True)
        self.chooser.clear()
        self.chooser.addItem("新建手动记忆", None)
        for row in self.rows:
            self.chooser.addItem(row["payload"].get("text", "")[:70], row["uid"])
        index = self.chooser.findData(selected) if selected else 0
        self.chooser.setCurrentIndex(max(index, 0))
        self.chooser.blockSignals(False)
        self.load()

    def load(self, _index=None):
        if not self._active():
            return
        uid = self.chooser.currentData()
        self.row = self.repo.db.visible("memories", uid) if uid else None
        old = self.row["payload"] if self.row else {}
        self.text.setPlainText(old.get("text", ""))
        self.kind.setCurrentIndex(max(0, self.kind.findData(old.get("kind", "fact"))))
        self.reason.setText("" if self.row else "手动创建")
        sources = old.get("source_uids", [])
        self.sources.setText("原文来源：" + "、".join(sources) if sources else "来源：用户手动陈述")
        if not self.row:
            message = "新记忆默认待确认。"
        elif self.row.get("dirty"):
            message = ("本地已请求确认，待上传；服务端状态尚未更新。" if old.get("status") == "confirmed" else
                       "本地待确认 · 待上传；请先同步新增或更正，再单独确认事实。")
        else:
            message = "服务端已确认" if old.get("status") == "confirmed" else "服务端待确认"
        self.status.setText(message)
        for button in (self.pending_button, self.delete_button, self.history_button):
            button.setEnabled(bool(self.row))
        self._confirmation_state()

    def _confirmation_state(self, *_args):
        row = self.row
        self.confirm_button.setEnabled(bool(row and not row.get("dirty") and row["version"] > 0 and row["payload"].get("kind") == "fact" and
            self.kind.currentData() == "fact" and self.text.toPlainText().strip() == row["payload"].get("text")))

    def _saved(self, uid):
        self.refresh(uid)
        if any(r["kind"] == "memories" and r["uid"] == uid for r in self.repo.db.conflicts()):
            self.status.setText("本地与远端版本冲突，均已保留；请在云账号设置中处理冲突。")
        self.panel.refresh()

    def save(self):
        try:
            self._require_active()
            fields = dict(kind=self.kind.currentData(), change_reason=self.reason.text(), expected_identity=self.identity)
            if self.row:
                uid = self.row["uid"]
                self.service.save_memory(uid, text=self.text.toPlainText(), expected_version=self.row["version"], **fields)
            else:
                uid = self.service.create_memory(self.profile_uid, self.text.toPlainText(), **fields)
            self._saved(uid)
        except Exception as exc:
            if self._active():
                self.status.setText(str(exc))

    def confirm(self):
        try:
            self._require_active()
            if not self.row or not self.confirm_button.isEnabled():
                return
            if QMessageBox.question(self, "确认这条事实", "请核对内容与来源。你是否明确确认这是一条事实？") != QMessageBox.StandardButton.Yes:
                return
            self.service.save_memory(self.row["uid"], status="confirmed", change_reason=self.reason.text() or "用户明确确认事实",
                expected_version=self.row["version"], expected_identity=self.identity)
            self._saved(self.row["uid"])
        except Exception as exc:
            if self._active():
                self.status.setText(str(exc))

    def pending(self):
        try:
            self._require_active()
            if self.row:
                self.service.save_memory(self.row["uid"], status="pending", change_reason=self.reason.text() or "重新核对",
                    expected_version=self.row["version"], expected_identity=self.identity)
                self._saved(self.row["uid"])
        except Exception as exc:
            if self._active():
                self.status.setText(str(exc))

    def delete(self):
        try:
            self._require_active()
            if self.row and QMessageBox.question(self, "删除记忆", "确定删除这条记忆？同步后将不能从历史恢复已删除内容。") == QMessageBox.StandardButton.Yes:
                self.service.delete_memory(self.row["uid"], confirmed=True, expected_version=self.row["version"], expected_identity=self.identity)
                self.refresh()
                self.panel.refresh()
        except Exception as exc:
            if self._active():
                self.status.setText(str(exc))

    def history(self):
        try:
            self._require_active()
            if self.row:
                uid = self.row["uid"]
                dialog = MemoryHistoryDialog(self.panel, uid, on_restored=lambda: self._saved(uid) if self._active() else None)
                dialog.open()
        except Exception as exc:
            self.status.setText(str(exc))


class EvidenceDialog(_AccountDialog):
    def __init__(self, panel, result):
        super().__init__(panel, result["profile_uid"])
        self.result, self.snapshot, self.claims = dict(result), None, []
        self.setWindowTitle("分析证据 · 分析当时的原文")
        note = QLabel("以下来源为分析当时的已核对原文快照；之后的消息更正不会改写此快照。有引用不代表结论一定正确。")
        note.setWordWrap(True)
        self.chooser, self.view = QComboBox(), QPlainTextEdit()
        self.view.setReadOnly(True)
        self.reload_button, self.save_button = QPushButton("重新读取证据"), QPushButton("将所选结论存为待确认记忆")
        self.save_button.setEnabled(False)
        for widget in (note, self.chooser, self.view, self.status, self.reload_button, self.save_button):
            self.layout.addWidget(widget)
        self.chooser.currentIndexChanged.connect(self.render)
        self.reload_button.clicked.connect(self.reload)
        self.save_button.clicked.connect(self.save_claim)
        self.reload()

    def clear_data(self):
        self.result, self.snapshot, self.claims = {}, None, []
        self.save_button.setEnabled(False)

    def _active(self):
        if not super()._active():
            return False
        result = getattr(self, "result", {})
        for kind, uid in (("analyses", result.get("analysis_id")), ("conversations", result.get("conversation_uid"))):
            row = self.repo.db.visible(kind, uid) if uid else None
            if row and row.get("deleted_at"):
                return False
        for message in (getattr(self, "snapshot", None) or {}).get("messages", []):
            row = self.repo.db.visible("messages", message.get("uid"))
            if row and row.get("deleted_at"):
                return False
        return True

    def reload(self):
        try:
            self._require_active()
            if self.busy:
                return
            self.claims, self.snapshot = [], None
            self.chooser.clear()
            self.view.clear()
            self.save_button.setEnabled(False)
            result = self.result
            self._read(lambda: self.service.evidence(result["analysis_id"], self.profile_uid, expected_identity=self.identity,
                       conversation_uid=result.get("conversation_uid"), context_revision=result.get("context_revision")), self.loaded)
        except Exception as exc:
            if self._active():
                self.status.setText(str(exc))

    def loaded(self, snapshot):
        self.snapshot, self.claims = snapshot, snapshot.get("evidence", [])
        self._require_active()
        if not self.claims:
            legacy = self.result.get("cloud_result") or self.result
            self.claims = evidence_for_ui({key: legacy.get(key, []) for key in ("facts", "hypotheses", "unknowns")})
        for claim in self.claims:
            self.chooser.addItem(f"[{_KINDS.get(claim['kind'], '未知')}] {claim['text'][:90]}")
        self.status.setText("出处来自分析快照，可逐条核对。" if self.claims else "此结果没有可用证据；旧版分析内容仍可在资料页查看。")
        self.render()

    def render(self, _index=None):
        index = self.chooser.currentIndex()
        if not self._active() or not 0 <= index < len(self.claims):
            self.save_button.setEnabled(False)
            return
        claim = self.claims[index]
        lines = ["分析当时的原文 · 上下文版本 " + str(self.snapshot.get("context_revision", "")),
                 _KINDS[claim["kind"]] + "：" + claim["text"], "有原文引用（请自行判断结论）" if
                 claim.get("citation_status") == "verified" else "引用不可用：没有可核对的原文出处。"]
        for quote in claim.get("source_quotes", []):
            speaker = "我" if quote.get("role") == "me" else "对方" if quote.get("role") in ("other", "her") else "未知发言者"
            lines.extend(["", speaker + " · " + quote.get("observed_at", ""), "“" + quote["quote"] + "”",
                          "消息 " + quote["message_uid"] + " · 分析时版本 " + str(quote.get("version", ""))])
        self.view.setPlainText("\n".join(lines))
        self.save_button.setEnabled(True)

    def save_claim(self):
        try:
            self._require_active()
            index = self.chooser.currentIndex()
            if not 0 <= index < len(self.claims):
                return
            claim = self.claims[index]
            sources = claim.get("source_message_uids", [])
            if not sources and QMessageBox.question(self, "没有可用引用", "此结论没有可核对原文。是否以你的手动陈述保存为待确认记忆？") != QMessageBox.StandardButton.Yes:
                return
            self.service.create_memory(self.profile_uid, claim["text"], kind="fact" if claim["kind"] == "fact" else "hypothesis",
                source_uids=sources, change_reason="从分析结论保存，等待用户核对", expected_identity=self.identity)
            self.status.setText("已保存为待确认记忆 · 本地待上传；可在“长期记忆”中更正或确认。")
            self.panel.refresh()
        except Exception as exc:
            if self._active():
                self.status.setText(str(exc))


class MemoryHistoryDialog(_AccountDialog):
    def __init__(self, panel, uid, on_restored=None):
        row = current().require().db.visible("memories", uid)
        if not row or row.get("deleted_at"):
            raise ValueError("memory_deleted")
        super().__init__(panel, row["payload"]["profile_uid"])
        self.uid, self.versions, self.before_version, self.on_restored = uid, [], 0, on_restored
        self.setWindowTitle("记忆历史 · 恢复会创建新的待确认版本")
        self.chooser, self.view = QComboBox(), QPlainTextEdit()
        self.view.setReadOnly(True)
        self.more_button, self.restore_button = QPushButton("加载更早版本"), QPushButton("恢复所选内容为新的待确认版本…")
        self.restore_button.setEnabled(False)
        for widget in (QLabel("历史需要联网。恢复使用当前版本号提交，不会回退历史；来源会重新核对。"),
                       self.chooser, self.view, self.status, self.more_button, self.restore_button):
            self.layout.addWidget(widget)
        self.chooser.currentIndexChanged.connect(self.render)
        self.more_button.clicked.connect(self.load_more)
        self.restore_button.clicked.connect(self.restore)
        self.load_more()

    def clear_data(self):
        self.versions, self.on_restored = [], None
        self.restore_button.setEnabled(False)

    def _active(self):
        if not super()._active():
            return False
        uid = getattr(self, "uid", None)
        memory = self.repo.db.visible("memories", uid) if uid else None
        if uid and (not memory or memory.get("deleted_at")):
            return False
        for revision in getattr(self, "versions", []):
            for source in revision.get("payload", {}).get("source_uids", []):
                row = self.repo.db.visible("messages", source)
                if row and row.get("deleted_at"):
                    return False
        return True

    def load_more(self):
        try:
            if self.busy:
                return
            before = self.before_version
            self.more_button.setEnabled(False)
            self._read(lambda: self.service.memory_history(self.uid, before_version=before, limit=50, expected_identity=self.identity), self.loaded)
        except Exception as exc:
            if self._active():
                self.status.setText(str(exc))

    def read_failed(self, error):
        super().read_failed(error)
        self.more_button.setEnabled(self._active())

    def loaded(self, page):
        known = {row["version"] for row in self.versions}
        for revision in page["versions"]:
            if revision["version"] not in known:
                self.versions.append(revision)
                known.add(revision["version"])
                self.chooser.addItem("版本 " + str(revision["version"]) + " · " + revision.get("updated_at", ""))
        self._require_active()
        cursor = page.get("next_before_version", 0)
        more = bool(page.get("has_more"))
        if more and (not isinstance(cursor, int) or cursor <= 0 or (self.before_version and cursor >= self.before_version)):
            raise RuntimeError("invalid_history_cursor")
        self.before_version = cursor
        self.more_button.setEnabled(more)
        self.status.setText("已读取 " + str(len(self.versions)) + " 个历史版本。")
        self.render()

    def render(self, _index=None):
        index = self.chooser.currentIndex()
        if not self._active() or not 0 <= index < len(self.versions):
            return
        revision = self.versions[index]
        old = revision["payload"]
        self.view.setPlainText("版本 " + str(revision["version"]) + "\n" + old.get("text", "") + "\n\n类型：" +
            _KINDS.get(old.get("kind"), "未知") + " · 当时状态：" + ("已确认" if old.get("status") == "confirmed" else "待确认") +
            "\n修改原因：" + old.get("change_reason", "") + "\n来源：" + ("、".join(old.get("source_uids", [])) or "用户手动陈述"))
        self.restore_button.setEnabled(True)

    def restore(self):
        try:
            self._require_active()
            index = self.chooser.currentIndex()
            if not 0 <= index < len(self.versions):
                return
            revision = self.versions[index]
            if QMessageBox.question(self, "恢复旧内容", "将版本 " + str(revision["version"]) + " 的内容作为新的待确认修改提交。确认继续？") != QMessageBox.StandardButton.Yes:
                return
            self.service.restore_memory(self.uid, revision, expected_identity=self.identity)
            self.status.setText("旧内容已作为新的待确认修改保存 · 待上传；同步后会生成新的历史版本。")
            if self.on_restored:
                self.on_restored()
            self.panel.refresh()
        except Exception as exc:
            if self._active():
                self.status.setText(str(exc))
