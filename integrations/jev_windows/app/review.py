# -*- coding: utf-8 -*-
"""核对本轮对话：和主界面同一套外观（暖白底、白卡片、深绿字、无边框圆角），
底部可以「累计聊天记录」——把采集到的最新记录并进列表（不覆盖你改过的行），累计之后再分析。"""
from PySide6.QtCore import Qt
import uuid
from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QInputDialog, QVBoxLayout
from qfluentwidgets import (FluentIcon as FIF, PlainTextEdit, PrimaryPushButton,
                            PushButton, setCustomStyleSheet)

from app.overlay import _MUTED, _Surface, _TitleBar, _label, _tool
from core.transcript import (format_review_rows, merge_reviewed_transcript,
                             merge_transcript, parse_review_rows)
from app.local_db import normalize_current_intent


class ReviewDialog(QDialog):
    def __init__(self, title: str, messages: list, parent=None, history_of=None, revision_of=None, *,
                 profiles=None, selected_profile_uid=None):
        super().__init__(parent)
        self._history_of = history_of  # 取该会话最新累计记录的可调用对象 → [(who, text, name)]
        self._revision_of = revision_of
        self.reviewed_revision = revision_of() if revision_of else None
        self._source_rows = list(messages)
        self.selected_profile_uid = selected_profile_uid
        self.current_intent = ""
        self.new_profile = None
        self._new_profiles = {}
        self.personBox = self.intentEditor = None
        self.setWindowTitle(f"核对本轮对话 · {title}")  # 无障碍/任务栏标题保留
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setStyleSheet(
            "QDialog { background: #f5f7f6; border: 1px solid #dce3de; border-radius: 14px; }"
        )
        width = parent.width() if parent is not None else 440  # 宽度跟主页面一致
        self.resize(max(360, width), 740 if profiles is not None else 600)
        if parent is not None:
            geo = parent.geometry()
            self.move(geo.x() + max(0, (geo.width() - self.width()) // 2), geo.y() + 40)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)
        header = _TitleBar(self)
        head = QHBoxLayout(header)
        head.setContentsMargins(18, 12, 10, 8)
        head.addWidget(_label("核对本轮对话", 18, "#24382d", True), 1)
        head.addWidget(_tool(FIF.CLOSE, "关闭", self.reject, header))
        outer.addWidget(header)

        body = QVBoxLayout()
        body.setContentsMargins(20, 4, 20, 16)
        body.setSpacing(10)
        body.addWidget(_label(f"会话：{title}", 13, "#304c3c", True))
        if profiles is not None:
            body.addWidget(_label("本轮人物", 13, "#304c3c", True))
            person_row = QHBoxLayout()
            self.personBox = QComboBox()
            self.personBox.setAccessibleName("本轮人物")
            self.personBox.addItem("请选择已有联系人", None)
            for row in profiles:
                self.personBox.addItem(f"{row['payload'].get('name', '未命名')} · {row['uid'][:8]}", row["uid"])
            index = self.personBox.findData(selected_profile_uid) if selected_profile_uid else 0
            self.personBox.setCurrentIndex(max(0, index))
            person_row.addWidget(self.personBox, 1)
            self.newPersonButton = PushButton("新建人物")
            self.newPersonButton.clicked.connect(self._new_person)
            person_row.addWidget(self.newPersonButton)
            body.addLayout(person_row)
            body.addWidget(_label("同一个人的不同平台聊天可选同一人物；确认分析时关联，已有历史保留。", 11, _MUTED))
            body.addWidget(_label("本轮想法（可选）", 13, "#304c3c", True))
            self.intentEditor = PlainTextEdit()
            self.intentEditor.setAccessibleName("本轮想法")
            self.intentEditor.setPlaceholderText("例如：我想邀请她周末见面。最多 2000 字，只用于本次分析及后续改写。")
            self.intentEditor.setFixedHeight(76)
            body.addWidget(self.intentEditor)
        body.addWidget(_label(
            "每行以「我：」「对方：」或「对方（名字）：」开头；OCR 可能认错字、错分边或漏行，改好再分析。\n"
            "群聊里对方的消息会带发言人名字（可以改），分析时一并带给模型；单聊不带名字。\n"
            "上滑翻看旧消息后，点「累计聊天记录」将新采集内容并入本页，再核对并确认。", 12, _MUTED))

        editor_card = _Surface()
        card_box = QVBoxLayout(editor_card)
        card_box.setContentsMargins(14, 12, 14, 12)
        self.editor = PlainTextEdit()
        self.editor.setPlainText("\n".join(format_review_rows(messages)))
        self.editor.setStyleSheet("PlainTextEdit { background: transparent; border: none; }")
        card_box.addWidget(self.editor)
        body.addWidget(editor_card, 1)

        self.feedback = _label("", 12, _MUTED)
        body.addWidget(self.feedback)

        actions = QVBoxLayout()
        actions.setSpacing(8)
        self.accumulateButton = PushButton("累计聊天记录")
        self.accumulateButton.setToolTip("按对话顺序合并最新采集记录，保留已核对的文字；合并后请再次核对")
        self.accumulateButton.clicked.connect(self._accumulate)
        confirm = PrimaryPushButton("确认并分析")
        confirm.clicked.connect(self._confirm)
        cancel = PushButton("取消")
        cancel.clicked.connect(self.reject)
        for button in (self.accumulateButton, confirm, cancel):  # 全部按钮按主页面宽度整行铺满
            button.setMinimumWidth(0)
            actions.addWidget(button)
        body.addLayout(actions)
        outer.addLayout(body, 1)

        self.messages = []

    def _new_person(self):
        name, ok = QInputDialog.getText(self, "新建人物", "人物称呼（确认分析后保存）")
        if not ok:
            return
        name = name.strip()
        if not name or len(name) > 200:
            self._feedback("人物称呼需要 1 至 200 字。", error=True)
            return
        uid = str(uuid.uuid4())
        self.new_profile = self._new_profiles[uid] = dict(name=name)
        self.personBox.addItem(name + " · 本轮新建", uid)
        self.personBox.setCurrentIndex(self.personBox.count() - 1)

    def _feedback(self, text, error=False):
        color = "#b44832" if error else "#18794e"
        qss = f"BodyLabel {{ color: {color}; background: transparent; }}"
        setCustomStyleSheet(self.feedback, qss, qss)
        self.feedback.setText(text)

    def _accumulate(self):
        if not self._history_of:
            return
        latest = self._history_of()
        if not latest:
            self._feedback("还没有可累计的记录：先回主界面，在聊天窗口开着时点一次「采集」开关。", error=True)
            return
        current = parse_review_rows(self.editor.toPlainText().splitlines())
        merged = merge_reviewed_transcript(current, self._source_rows, latest)
        self._source_rows = merge_transcript(self._source_rows, latest, unmatched="newer")
        added = len(merged) - len(current)
        self.editor.setPlainText("\n".join(format_review_rows(merged)))
        if self._revision_of:
            self.reviewed_revision = self._revision_of()
        if added > 0:
            self._feedback(f"已累计：新增 {added} 条，共 {len(merged)} 条。")
        else:
            self._feedback("没有新的记录（列表已是最新）。")

    def _confirm(self):
        if self.personBox is not None:
            selected = self.personBox.currentData()
            if not selected:
                self._feedback("请明确选择本轮人物，或新建人物。", error=True)
                return
            try:
                intent = normalize_current_intent(self.intentEditor.toPlainText())
            except ValueError as exc:
                self._feedback(str(exc), error=True)
                return
            self.selected_profile_uid = selected
            self.new_profile = self._new_profiles.get(selected)
            self.current_intent = intent
        rows = parse_review_rows(self.editor.toPlainText().splitlines())
        parsed = []
        for who, content, name in rows:
            if who not in ("me", "her"):
                self._feedback("每行须以「我：」「对方：」或「对方（名字）：」开头（不需要的行直接删掉）。", error=True)
                return
            if not content:
                self._feedback("请删除空消息，或补上正文。", error=True)
                return
            parsed.append((who, content, name) if name else (who, content))
        if not parsed or len(parsed) > 60:
            self._feedback("请保留 1 到 60 条相关消息。", error=True)
            return
        self.messages = parsed
        self.accept()
