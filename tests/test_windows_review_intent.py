"""Offline regressions for choosing a person and one-analysis intent on Windows."""
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from integrations.jev_windows.app.local_db import LocalDatabase
from integrations.jev_windows.app.repositories import CloudRepository


class ReviewIntentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = LocalDatabase(Path(self.temp.name), "https://test.example", "one")
        self.addCleanup(self.db.close)
        self.gateway = Mock()
        self.repo = CloudRepository(self.db, self.gateway)

    def person(self, uid, name):
        self.db.save("profiles", uid, dict(name=name, stage="未填写", goal="自然接话", background="", notes=""))

    def test_placeholder_title_cannot_recover_or_persist_a_person_binding(self):
        self.person("p", "真人")
        for title in ("", "  ", "已确认对话", "未识别会话", "当前会话", "微信", "QQ", "连接中", "正在连接",
                      "加载中", "同步中", "connecting", "Loading", "SYNCING", "当前对话", "未命名对话", "未知",
                      "未识别", "聊天记录", "抖音", "douyin", "未连接", "正在连接…", "Loading..."):
            with self.subTest(title=title), self.assertRaises(ValueError):
                self.repo.bind_chat("wechat", title, "p", "device")
        self.assertEqual(self.db.list_records("identity_links"), [])

    def test_review_context_does_not_materialize_missing_legacy_conversation(self):
        self.person("p", "真人")
        identity = self.db.stable_uid("binding:device:wechat:聊天")
        self.db.save("identity_links", identity, dict(profile_uid="p", platform="wechat", legacy_id="聊天", source_device="device"))
        before = self.db.operations()
        writes = self.db.conn.total_changes
        profile, conversation = self.repo.review_context("wechat", "聊天", "device")
        self.assertEqual(self.db.conn.total_changes, writes, "review must not write legacy UID mappings either")
        self.assertEqual(profile, "p")
        self.assertEqual(conversation, self.db.stable_uid("conversation:device:wechat:聊天:p"))
        self.assertEqual(self.db.list_records("conversations"), [])
        self.assertEqual(self.db.operations(), before)
        self.assertEqual(self.repo.conversation_for("wechat", "聊天", "device"), (profile, conversation))
        self.assertIsNotNone(self.db.visible("conversations", conversation), "confirmation's lookup can still materialize it")

    def test_cancel_review_keeps_profiles_conversations_bindings_and_outbox_unchanged(self):
        from test_windows_capture_flow import load_main
        main = load_main()
        main.settings.cloud_mode.return_value = True
        service = Mock(identity="generation", source_device="device")
        service.require.return_value = self.repo
        self.person("p", "真人")
        identity = self.db.stable_uid("binding:device:wechat:我是好人")
        self.db.save("identity_links", identity, dict(profile_uid="p", platform="wechat", legacy_id="我是好人", source_device="device"))
        before = {kind: self.db.list_records(kind) for kind in ("profiles", "conversations", "identity_links", "messages")}
        operations = self.db.operations()
        writes = self.db.conn.total_changes
        class Dialog:
            def __init__(self, *args, **kwargs):
                self.new_profile = {"name": "取消时丢弃的草稿"}
            def exec(self):
                return False
        main.ReviewDialog = Dialog
        with patch.dict(sys.modules, {"app.cloud_runtime": Mock(current=Mock(return_value=service))}), \
                patch.object(main.threading, "Thread") as thread:
            main.start_analyze("我是好人", [("her", "周末有空")])
        self.assertEqual({kind: self.db.list_records(kind) for kind in before}, before)
        self.assertEqual(self.db.operations(), operations)
        self.assertEqual(self.db.conn.total_changes, writes)
        thread.assert_not_called()

    def test_unbound_review_query_does_not_even_write_uid_mapping(self):
        self.person("p", "真人")
        writes = self.db.conn.total_changes
        self.assertEqual(self.repo.review_context("wechat", "未绑定的会话", "device"), (None, None))
        self.assertEqual(self.db.conn.total_changes, writes)

    def test_explicit_selection_changes_future_binding_without_moving_history(self):
        self.person("p", "甲")
        self.person("q", "乙")
        old = self.repo.bind_chat("wechat", "聊天", "p", "device")
        self.db.confirm_messages(old, "p", [("her", "甲的旧消息")])
        self.assertTrue(hasattr(self.repo, "confirm_person"), "review confirmation needs an explicit person choice")
        profile, new = self.repo.confirm_person("wechat", "聊天", "q", "device")
        self.assertEqual(profile, "q")
        self.assertNotEqual(old, new)
        self.assertEqual(self.repo.conversation_for("wechat", "聊天", "device"), ("q", new))
        self.assertEqual(self.db.list_records("messages")[0]["payload"]["profile_uid"], "p")

    def test_separate_untitled_reviews_never_share_binding_or_conversation(self):
        self.person("p", "真人")
        self.assertTrue(hasattr(self.repo, "confirm_person"), "untitled reviews need an isolated conversation")
        first = self.repo.confirm_person("wechat", "已确认对话", "p", "device")
        second = self.repo.confirm_person("wechat", "已确认对话", "p", "device")
        self.assertNotEqual(first[1], second[1])
        self.assertEqual(self.db.list_records("identity_links"), [])
        with self.assertRaises(ValueError):
            self.repo.conversation_for("wechat", "已确认对话", "device")

    def test_intent_is_trimmed_persisted_and_distinguishes_requests_across_restart(self):
        self.assertIn("current_intent", __import__("inspect").signature(self.db.prepare_job).parameters)
        old = self.db.prepare_job("c", 3, "analysis")
        blank = self.db.prepare_job("c", 3, "analysis", current_intent=" \n ")
        self.assertEqual(old, blank)
        self.assertNotIn("current_intent", blank["body"])
        first = self.db.prepare_job("c", 3, "analysis", current_intent="  想约周末见面\n")
        other = self.db.prepare_job("c", 3, "analysis", current_intent="想婉拒")
        reopened = LocalDatabase(Path(self.temp.name), "https://test.example", "one")
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.prepare_job("c", 3, "analysis", current_intent="想约周末见面"), first)
        self.assertEqual(first["body"]["current_intent"], "想约周末见面")
        self.assertNotEqual(first["request_id"], old["request_id"])
        self.assertNotEqual(first["request_id"], other["request_id"])
        self.assertEqual(self.db.operations(), [])

    def test_latest_job_shortcut_does_not_replay_a_different_intent(self):
        self.assertIn("current_intent", __import__("inspect").signature(self.repo.run_job).parameters)
        old = self.db.prepare_job("c", 3, "analysis", current_intent="约见面")
        self.gateway.request.side_effect = RuntimeError("network_ambiguous")
        for intent in ("约见面", "婉拒", ""):
            with self.assertRaisesRegex(RuntimeError, "network_ambiguous"):
                self.repo.run_job("c", 3, current_intent=intent)
        bodies = [call.args[2] for call in self.gateway.request.call_args_list]
        self.assertEqual(bodies[0]["request_id"], old["request_id"])
        self.assertEqual(bodies[1]["current_intent"], "婉拒")
        self.assertNotIn("current_intent", bodies[2])
        self.assertEqual(len({body["request_id"] for body in bodies}), 3)

    def test_deleted_intent_job_blocks_implicit_resubmission_after_preferences_or_intent_change(self):
        from test_cloud_windows import record
        self.db.save("preferences", "00000000-0000-4000-8000-000000000001", dict(style="原偏好"))
        pending = self.db.prepare_job("c", 3, "analysis", preferences=self.repo.preferences(), current_intent="删除后不可恢复的意图")
        self.db.update_job(pending["request_id"], dict(analysis_id="deleted-analysis", status="succeeded"))
        self.db.apply_pull([dict(seq=1, record=record("analyses", "deleted-analysis", {}, 2, "deleted"))], "deleted")
        self.db.save("preferences", "00000000-0000-4000-8000-000000000001", dict(style="新偏好"))
        self.gateway.request.side_effect = RuntimeError("network_ambiguous")
        before = self.db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        for intent in ("删除后不可恢复的意图", "不同意图", ""):
            with self.subTest(intent=intent), self.assertRaisesRegex(RuntimeError, "analysis_deleted"):
                self.repo.run_job("c", 3, current_intent=intent)
        self.gateway.request.assert_not_called()
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], before)
        self.assertNotIn("删除后不可恢复的意图", self.db.conn.execute("SELECT body || job FROM jobs").fetchone()[0])

    def test_intent_limit_is_enforced_before_writing_request(self):
        self.assertIn("current_intent", __import__("inspect").signature(self.db.prepare_job).parameters)
        with self.assertRaises(ValueError):
            self.db.prepare_job("c", 3, "analysis", current_intent="字" * 2001)
        self.assertIsNone(self.db.latest_job("c", 3, "analysis"))
        good = self.db.prepare_job("c", 3, "analysis", current_intent="字" * 2000)
        self.assertEqual(len(good["body"]["current_intent"]), 2000)

    def test_raw_intent_length_limit_cannot_be_bypassed_with_whitespace(self):
        with self.assertRaises(ValueError):
            self.db.prepare_job("c", 3, "analysis", current_intent=" " * 2001)
        self.assertIsNone(self.db.latest_job("c", 3, "analysis"))

    def test_confirmed_intent_only_lives_in_job_and_followup_uses_parent(self):
        from integrations.jev_windows.app.cloud_runtime import CloudSession
        from test_cloud_windows import record
        vault = Mock()
        vault.load.return_value = dict(token="synthetic", device=dict(id="d"), device_secret="s", user=dict(id="one"))
        service = CloudSession("https://test.example", Path(self.temp.name) / "runtime", vault=vault)
        self.addCleanup(service.close)
        repo = service.require()
        repo.db.save("profiles", "p", dict(name="真人", stage="了解中", goal="自然接话", background="", notes=""))
        def sync():
            receipts = [dict(op_id=op["op_id"], status="applied", record=record(op["entity_type"], op["entity_uid"],
                {**op["payload"], **({"context_revision": 1} if op["entity_type"] == "conversations" else {})}))
                for op in repo.db.operations()]
            repo.db.apply_push(receipts)
        repo.syncer.sync = sync
        submitted = []
        def request(method, path, body=None):
            if method == "POST":
                submitted.append(body)
                return dict(analysis_id="analysis-" + str(len(submitted)), status="queued")
            current = submitted[-1]
            uid = "analysis-" + str(len(submitted))
            result = dict(analysis_id=uid, conversation_uid=current["conversation_uid"],
                context_revision=current["context_revision"], candidates=[dict(candidate_id="a", text="候选", weight=None)])
            return dict(analysis_id=uid, conversation_uid=current["conversation_uid"], context_revision=current["context_revision"],
                        status="succeeded", result=result)
        service.gateway.request = request
        result = service.analyze("wechat", "聊天", [("her", "周末有空")], None, expected_identity=service.identity,
            expected_context=(None, None), selected_profile_uid="p", current_intent="  想约周末见面  ")
        self.assertEqual(submitted[0]["current_intent"], "想约周末见面")
        for kind in ("profiles", "notes", "preferences", "memories", "conversations", "messages"):
            self.assertNotIn("想约周末见面", json.dumps(repo.db.list_records(kind), ensure_ascii=False))
        service.extra({**result, "cloud_identity": service.identity}, "rewrite", 0)
        self.assertNotIn("current_intent", submitted[-1])
        self.assertEqual(submitted[-1]["parent_analysis_id"], result["analysis_id"])

    def test_selection_cannot_override_a_binding_changed_while_review_was_open(self):
        from integrations.jev_windows.app.cloud_runtime import CloudSession
        vault = Mock()
        vault.load.return_value = dict(token="synthetic", device=dict(id="d"), device_secret="s", user=dict(id="one"))
        service = CloudSession("https://test.example", Path(self.temp.name) / "runtime", vault=vault)
        self.addCleanup(service.close)
        repo = service.require()
        for uid in ("p", "q"):
            repo.db.save("profiles", uid, dict(name=uid))
        before = repo.review_context("wechat", "聊天", service.source_device)
        repo.bind_chat("wechat", "聊天", "q", service.source_device)
        service.gateway.request = Mock()
        with self.assertRaisesRegex(RuntimeError, "context_changed"):
            service.analyze("wechat", "聊天", [("her", "原文")], None, expected_identity=service.identity,
                expected_context=before, selected_profile_uid="p")
        self.assertEqual(repo.db.list_records("messages"), [])
        self.assertEqual(repo.conversation_for("wechat", "聊天", service.source_device)[0], "q")
        service.gateway.request.assert_not_called()

    def test_cloud_review_opens_without_an_existing_binding_and_forwards_confirmed_choice(self):
        from test_windows_capture_flow import load_main
        main = load_main()
        main.settings.cloud_mode.return_value = True
        service = Mock(identity="generation", source_device="device")
        service.require.return_value = self.repo
        self.person("p", "真人")
        seen = []
        class Dialog:
            selected_profile_uid = "p"
            new_profile = None
            current_intent = "想邀请她周末见面"
            reviewed_revision = 0
            messages = [("her", "周末有空")]
            def __init__(self, *args, **kwargs):
                seen.append(kwargs)
            def exec(self):
                return True
        main.ReviewDialog = Dialog
        with patch.dict(sys.modules, {"app.cloud_runtime": Mock(current=Mock(return_value=service))}), \
                patch.object(main.threading, "Thread") as thread:
            main.start_analyze("我是好人", [("her", "周末有空")])
        self.assertEqual(len(seen), 1, "unbound chats must reach the review instead of account settings")
        self.assertEqual(seen[0]["profiles"][0]["uid"], "p")
        self.assertIsNone(seen[0]["selected_profile_uid"])
        self.assertEqual(thread.call_args.kwargs["kwargs"]["selected_profile_uid"], "p")
        self.assertEqual(thread.call_args.kwargs["kwargs"]["current_intent"], "想邀请她周末见面")
        self.assertEqual(self.db.list_records("identity_links"), [], "selection applies only after confirmation")

    def test_real_qt_review_requires_choice_and_keeps_new_person_as_unsaved_draft(self):
        windows = Path(__file__).resolve().parents[1] / "integrations/jev_windows"
        code = '''
import sys
from unittest.mock import patch
from PySide6.QtWidgets import QApplication, QDialog
from app.review import ReviewDialog
app = QApplication.instance() or QApplication([])
profiles = [dict(uid="p", payload=dict(name="甲")), dict(uid="q", payload=dict(name="乙"))]
d = ReviewDialog("聊天", [("her", "周末有空")], profiles=profiles, selected_profile_uid="p")
assert d.personBox.currentData() == "p"
d.personBox.setCurrentIndex(2)
d.intentEditor.setPlainText("  我想邀请她周末见面  ")
d._confirm()
assert d.result() == QDialog.Accepted and d.selected_profile_uid == "q"
assert d.current_intent == "我想邀请她周末见面"
missing = ReviewDialog("已确认对话", [("her", "你好")], profiles=profiles)
missing._confirm()
assert missing.result() != QDialog.Accepted
with patch("app.review.QInputDialog.getText", return_value=("新人", True)):
    missing.newPersonButton.click()
assert missing.new_profile["name"] == "新人"
assert len(profiles) == 2
missing.intentEditor.setPlainText("字" * 2001)
missing._confirm()
assert missing.result() != QDialog.Accepted
direct = ReviewDialog("聊天", [("her", "你好")])
assert direct.intentEditor is None and direct.personBox is None
print("QT_REVIEW_OK")
'''
        env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONPATH": str(windows)}
        result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("QT_REVIEW_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
