"""Real Windows repository and Qt memory workflows with synthetic account data only."""
import importlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_cloud_windows import module, record


def memory_payload(text="原事实", kind="fact", status="confirmed", sources=None):
    return dict(profile_uid="p", text=text, kind=kind, status=status,
                source_uids=["m"] if sources is None else sources,
                source_type="messages" if sources is None or sources else "user",
                source_refs=[dict(message_uid="m", version=1)] if sources is None else [], change_reason="已核对")


class MemoryWindowsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = module("local_db").LocalDatabase(self.root, "https://api.example.test", "one")
        self.addCleanup(self.db.close)
        rows = [record("profiles", "p", {"name": "甲"}), record("profiles", "q", {"name": "乙"}),
                record("messages", "m", dict(profile_uid="p", conversation_uid="c", text="周六见", role="her",
                                             confirmed=True, observed_at="2026-10-21T01:00:00Z")),
                record("messages", "other", dict(profile_uid="q", conversation_uid="other-c", text="其他人", confirmed=True)),
                record("messages", "unreviewed", dict(profile_uid="p", text="未核对", confirmed=False)),
                record("memories", "memo", memory_payload(), version=4)]
        self.db.apply_pull([dict(seq=i + 1, record=row) for i, row in enumerate(rows)], "seed")
        self.gateway = Mock()
        self.repo = module("repositories").CloudRepository(self.db, self.gateway)

    def feature(self, name):
        self.assertTrue(callable(getattr(self.repo, name, None)), name + " must be available in the Windows workflow")
        return getattr(self.repo, name)

    def test_manual_memory_is_pending_and_offline_operation_survives_restart(self):
        uid = self.feature("create_memory")("p", "手动补充", kind="hypothesis", change_reason="用户录入")
        pending = self.db.visible("memories", uid)
        self.assertEqual(pending["payload"], dict(profile_uid="p", text="手动补充", kind="hypothesis", status="pending",
                                                source_uids=[], source_type="user", change_reason="用户录入"))
        self.assertTrue(pending["dirty"])
        reopened = module("local_db").LocalDatabase(self.root, "https://api.example.test", "one")
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.operations()[0]["base_version"], 0)
        self.assertEqual(reopened.operations()[0]["payload"]["status"], "pending")
        self.gateway.request.assert_not_called()

    def test_claim_save_revalidates_local_profile_sources_and_never_auto_confirms(self):
        create = self.feature("create_memory")
        uid = create("p", "约在周六", kind="fact", source_uids=["m", "m"])
        self.assertEqual(self.db.visible("memories", uid)["payload"]["source_uids"], ["m"])
        self.assertEqual(self.db.visible("memories", uid)["payload"]["status"], "pending")
        for source in ("other", "missing", "unreviewed"):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "source"):
                create("p", "不能引入别人的资料", source_uids=[source])
        self.assertEqual(len(self.db.operations()), 1)

    def test_memory_edit_requires_reason_drops_server_proof_and_stale_edit_conflicts(self):
        save = self.feature("save_memory")
        with self.assertRaisesRegex(ValueError, "reason"):
            save("memo", text="更正", change_reason="", expected_version=4)
        self.db.apply_pull([dict(seq=7, record=record("memories", "memo", memory_payload("另一端"), version=5))], "new")
        save("memo", text="我的更正", kind="fact", change_reason="核对后修改", expected_version=4)
        self.assertEqual(self.db.operations(), [])
        conflict = self.db.conflicts()[0]
        self.assertEqual(conflict["remote"]["version"], 5)
        self.assertEqual(conflict["local"]["text"], "我的更正")
        self.assertEqual(conflict["local"]["status"], "pending")
        self.assertNotIn("source_refs", conflict["local"])

    def test_hypothesis_requires_separate_conversion_before_fact_confirmation(self):
        save = self.feature("save_memory")
        self.db.apply_pull([dict(seq=7, record=record("memories", "hyp", memory_payload(kind="hypothesis", status="pending")))], "hyp")
        with self.assertRaisesRegex(ValueError, "hypothesis"):
            save("hyp", status="confirmed", change_reason="确认", expected_version=1)
        with self.assertRaisesRegex(ValueError, "separate"):
            save("hyp", kind="fact", status="confirmed", change_reason="同时转换确认", expected_version=1)
        save("hyp", kind="fact", change_reason="用户明确转换为事实", expected_version=1)
        self.assertEqual(self.db.visible("memories", "hyp")["payload"]["status"], "pending")
        with self.assertRaisesRegex(ValueError, "sync_required"):
            save("hyp", status="confirmed", change_reason="用户明确确认事实", expected_version=1)
        conversion = self.db.operations()[0]
        self.db.apply_push([dict(op_id=conversion["op_id"], status="applied",
                                record=record("memories", "hyp", conversion["payload"], version=2))])
        save("hyp", status="confirmed", change_reason="用户明确确认事实", expected_version=2)
        self.assertEqual(self.db.visible("memories", "hyp")["payload"]["status"], "confirmed")
        self.assertEqual(self.db.operations()[0]["base_version"], 2)
        save("hyp", status="pending", change_reason="重新核对", expected_version=2)
        self.assertEqual(self.db.visible("memories", "hyp")["payload"]["status"], "pending")

    def test_confirmation_cannot_coalesce_with_unsynced_text_correction(self):
        self.repo.save_memory("memo", text="更正后的事实", change_reason="修正原文", expected_version=4)
        before = self.db.operations()
        with self.assertRaisesRegex(ValueError, "sync_required"):
            self.repo.save_memory("memo", status="confirmed", change_reason="明确确认", expected_version=4)
        self.assertEqual(self.db.operations(), before)
        self.assertEqual(self.db.operations()[0]["payload"]["status"], "pending")

    def test_new_unsynced_fact_requires_sync_before_confirmation(self):
        uid = self.repo.create_memory("p", "新增事实", change_reason="手动创建")
        before = self.db.operations()
        with self.assertRaisesRegex(ValueError, "sync_required"):
            self.repo.save_memory(uid, status="confirmed", change_reason="明确确认", expected_version=0)
        self.assertEqual(self.db.operations(), before)
        self.assertEqual(self.db.visible("memories", uid)["payload"]["status"], "pending")

    def test_changed_fact_text_cannot_confirm_in_the_same_action(self):
        save = self.feature("save_memory")
        save("memo", text="更正后的事实", status="confirmed", change_reason="修改", expected_version=4)
        self.assertEqual(self.db.visible("memories", "memo")["payload"]["status"], "pending")

    def test_restore_uses_current_base_version_and_creates_pending_outbox_revision(self):
        restore = self.feature("restore_memory")
        old = record("memories", "memo", memory_payload("旧事实", status="confirmed"), version=2)
        restore("memo", old)
        op = self.db.operations()[0]
        self.assertEqual((op["base_version"], op["payload"]["text"], op["payload"]["status"], op["payload"]["change_reason"]),
                         (4, "旧事实", "pending", "恢复版本 2"))
        self.assertNotIn("source_refs", op["payload"])
        self.assertEqual(self.db.visible("memories", "memo")["version"], 4)

    def test_restore_refuses_deleted_sources_and_foreign_or_deleted_memories(self):
        restore = self.feature("restore_memory")
        old = record("memories", "memo", memory_payload(), version=2)
        self.db.apply_pull([dict(seq=7, record=record("messages", "m", {}, version=2, deleted_at="deleted"))], "gone")
        with self.assertRaisesRegex(ValueError, "source"):
            restore("memo", old)
        old["payload"]["profile_uid"] = "q"
        with self.assertRaisesRegex(ValueError, "profile"):
            restore("memo", old)
        self.db.apply_pull([dict(seq=8, record=record("memories", "memo", {}, version=5, deleted_at="deleted"))], "gone-memory")
        with self.assertRaisesRegex(ValueError, "deleted"):
            restore("memo", old)
        self.assertEqual(self.db.operations(), [])


    def test_history_fetch_uses_pagination_and_rejects_wrong_account_profile_payload(self):
        fetch = self.feature("memory_history")
        self.gateway.request.return_value = dict(memory_uid="memo", profile_uid="p", versions=[], has_more=True, next_before_version=3)
        self.assertTrue(fetch("memo", before_version=4, limit=2)["has_more"])
        self.assertEqual(self.gateway.request.call_args.args, ("GET", "/v1/memories/memo/history?before_version=4&limit=2"))
        self.gateway.request.return_value["profile_uid"] = "q"
        with self.assertRaisesRegex(RuntimeError, "context_mismatch"):
            fetch("memo")

    def test_delete_requires_explicit_confirmation_and_retains_optimistic_base(self):
        delete = self.feature("delete_memory")
        with self.assertRaisesRegex(ValueError, "confirmation"):
            delete("memo", confirmed=False, expected_version=4)
        self.assertEqual(self.db.operations(), [])
        delete("memo", confirmed=True, expected_version=4)
        self.assertEqual(self.db.operations()[0]["action"], "delete")
        self.assertEqual(self.db.operations()[0]["base_version"], 4)
        self.assertTrue(self.db.visible("memories", "memo")["deleted_at"])

    def test_evidence_mapping_keeps_original_quote_speaker_time_and_marks_missing_unavailable(self):
        mapping = module("gateway")
        self.assertTrue(callable(getattr(mapping, "evidence_for_ui", None)), "Evidence needs a snapshot-aware mapper")
        snapshot = dict(analysis_id="a", conversation_uid="c", profile_uid="p", context_revision=1,
            messages=[dict(uid="m", version=1, role="her", text="好，周六见", observed_at="2026-10-21T01:00:00Z")],
            evidence=[dict(claim_id="f1", kind="fact", text="周六见面", source_message_uids=["m"],
                           source_quotes=[dict(message_uid="m", quote="周六见")], citation_status="verified"),
                      dict(claim_id="h1", kind="hypothesis", text="可能有空", source_message_uids=["missing"],
                           source_quotes=[dict(message_uid="missing", quote="虚构引用")], citation_status="verified")])
        rows = mapping.evidence_for_ui(snapshot)
        quote = rows[0]["source_quotes"][0]
        self.assertEqual((quote["quote"], quote["role"], quote["observed_at"], quote["version"]),
                         ("周六见", "her", "2026-10-21T01:00:00Z", 1))
        self.assertEqual(rows[1]["citation_status"], "unavailable")
        self.assertEqual(rows[1]["source_quotes"], [])

    def test_legacy_result_remains_readable_with_unavailable_evidence(self):
        result = dict(analysis_id="old", conversation_uid="c", context_revision=1, candidates=[],
                      facts=["原事实"], hypotheses=["旧推测"], unknowns=["未说明"])
        mapped = module("gateway").result_for_ui(result)
        self.assertEqual(mapped["goutou"]["facts"], ["原事实"])
        self.assertIn("evidence", mapped, "Legacy claims must remain available with an explicit unavailable citation")
        self.assertEqual([row["citation_status"] for row in mapped["evidence"]], ["unavailable"] * 3)

    def test_late_evidence_and_history_response_is_rejected_after_account_change(self):
        runtime, auth = module("cloud_runtime"), module("auth_store")
        vault = auth.SessionStore(self.root / "vault", protect=lambda x: x[::-1], unprotect=lambda x: x[::-1])
        vault.save("https://api.example.test", dict(token="synthetic", device=dict(id="d"), device_secret="s", user=dict(id="one")))
        service = runtime.CloudSession("https://api.example.test", self.root / "session", vault=vault)
        self.addCleanup(service.close)
        service.repository = self.repo
        self.assertTrue(callable(getattr(service, "evidence", None)), "Evidence reads need account lifetime guards")
        self.assertTrue(callable(getattr(service, "memory_history", None)), "History reads need account lifetime guards")
        for operation in (lambda identity: service.evidence("a", "p", expected_identity=identity),
                          lambda identity: service.memory_history("memo", expected_identity=identity)):
            identity = service.identity
            def response(*_args, **_kwargs):
                service.identity = "changed-" + identity
                return dict(analysis_id="a", conversation_uid="c", profile_uid="p", context_revision=1, messages=[], evidence=[],
                            memory_uid="memo", versions=[], has_more=False, next_before_version=0)
            self.gateway.request.side_effect = response
            with self.assertRaisesRegex(RuntimeError, "account_changed"):
                operation(identity)
        self.assertEqual(self.db.operations(), [])


class MemoryWindowsQtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            raise unittest.SkipTest("Real Qt workflows run in ../windows-venv")
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        MemoryWindowsTests.setUp(self)
        windows_root = str(Path(__file__).resolve().parents[1] / "integrations/jev_windows")
        sys.path.insert(0, windows_root)
        self.addCleanup(sys.path.remove, windows_root)
        from integrations.jev_windows.core import providers
        windows_app = importlib.import_module("integrations.jev_windows.app")
        aliases = patch.dict(sys.modules, {"core.providers": providers, "app": windows_app})
        aliases.start()
        self.addCleanup(aliases.stop)
        self.ui = importlib.import_module("integrations.jev_windows.app.cloud_memory_ui")
        runtime, auth = module("cloud_runtime"), module("auth_store")
        vault = auth.SessionStore(self.root / "vault", protect=lambda x: x[::-1], unprotect=lambda x: x[::-1])
        vault.save("https://api.example.test", dict(token="synthetic", device=dict(id="d"), device_secret="s",
                                                   user=dict(id="one", email="one@example.test")))
        self.service = runtime.CloudSession("https://api.example.test", self.root / "session", vault=vault)
        self.addCleanup(self.service.close)
        self.service.repository = self.repo
        self.panel_module = module("cloud_ui")
        for target in (self.ui, self.panel_module):
            active = patch.object(target, "current", return_value=self.service)
            active.start()
            self.addCleanup(active.stop)
        active = patch.object(self.panel_module.settings, "cloud_mode", return_value=True)
        active.start()
        self.addCleanup(active.stop)
        from PySide6.QtWidgets import QWidget
        self.overlay = SimpleNamespace(settingsPage=QWidget(), win=QWidget(), on_settings_saved=None,
                                       invalidate_replies=Mock(), reset_chat_state=Mock(), _load_settings=Mock())
        self.panel = self.panel_module.CloudPanel(self.overlay)
        self.addCleanup(self.panel.timer.stop)
        self.addCleanup(self.panel.close)
        self.addCleanup(lambda: [dialog.reject() for dialog in list(self.panel.account_dialogs)])

    def wait_read(self, dialog):
        deadline = time.monotonic() + 3
        while dialog.busy and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertFalse(dialog.busy, "A synthetic read should finish without blocking Qt")
        self.app.processEvents()

    def test_qt_manual_edit_confirm_pending_and_delete_controls_stage_real_operations(self):
        dialog = self.ui.MemoryDialog(self.panel, "p")
        dialog.show()
        dialog.text.setPlainText("手动事实")
        dialog.reason.setText("用户录入")
        dialog.save_button.click()
        rows = [r for r in self.db.list_records("memories") if r["payload"]["text"] == "手动事实"]
        self.assertEqual(len(rows), 1)
        uid = rows[0]["uid"]
        self.assertIn("待上传", dialog.status.text())
        self.assertFalse(dialog.confirm_button.isEnabled())
        self.assertIn("先同步", dialog.status.text())
        creation = self.db.operations()[0]
        self.db.apply_push([dict(op_id=creation["op_id"], status="applied",
                                record=record("memories", uid, creation["payload"], version=1))])
        dialog.refresh(uid)
        with patch.object(self.ui.QMessageBox, "question", return_value=self.ui.QMessageBox.StandardButton.Yes):
            dialog.confirm_button.click()
        self.assertEqual(self.db.visible("memories", uid)["payload"]["status"], "confirmed")
        dialog.pending_button.click()
        self.assertEqual(self.db.visible("memories", uid)["payload"]["status"], "pending")
        with patch.object(self.ui.QMessageBox, "question", return_value=self.ui.QMessageBox.StandardButton.No):
            dialog.delete_button.click()
        self.assertIsNotNone(self.db.visible("memories", uid))
        with patch.object(self.ui.QMessageBox, "question", return_value=self.ui.QMessageBox.StandardButton.Yes):
            dialog.delete_button.click()
        self.assertTrue(self.db.visible("memories", uid)["deleted_at"])
        self.assertEqual(self.db.operations()[0]["action"], "delete")

    def test_qt_account_panel_has_a_working_memory_entry(self):
        self.assertTrue(hasattr(self.panel, "memory_button"), "Memory workflows must be reachable from account settings")
        self.panel.memory_button.click()
        dialogs = [dialog for dialog in self.panel.account_dialogs if isinstance(dialog, self.ui.MemoryDialog)]
        self.assertEqual(len(dialogs), 1)
        self.assertTrue(dialogs[0].isVisible())

    def test_qt_evidence_displays_snapshot_quote_and_saves_selected_claim_pending(self):
        self.gateway.request.return_value = dict(analysis_id="a", profile_uid="p", conversation_uid="c", context_revision=1,
            messages=[dict(uid="m", version=1, role="other", text="周六见", observed_at="2026-10-21T01:00:00Z")],
            evidence=[dict(claim_id="f", kind="fact", text="约在周六", source_message_uids=["m"],
                           source_quotes=[dict(message_uid="m", quote="周六见")], citation_status="verified")])
        dialog = self.ui.EvidenceDialog(self.panel, dict(analysis_id="a", profile_uid="p", conversation_uid="c", context_revision=1))
        dialog.show()
        self.wait_read(dialog)
        text = dialog.view.toPlainText()
        for expected in ("分析当时", "周六见", "对方", "2026-10-21T01:00:00Z", "有原文引用"):
            self.assertIn(expected, text)
        dialog.save_button.click()
        rows = [r for r in self.db.list_records("memories") if r["payload"]["text"] == "约在周六"]
        self.assertEqual(rows[0]["payload"]["status"], "pending")
        self.assertEqual(rows[0]["payload"]["source_uids"], ["m"])

    def test_qt_legacy_analysis_without_api_claims_shows_cached_claims_with_unavailable_citations(self):
        self.gateway.request.return_value = dict(analysis_id="legacy", profile_uid="p", conversation_uid="c", context_revision=1,
                                                 messages=[], evidence=[])
        dialog = self.ui.EvidenceDialog(self.panel, dict(analysis_id="legacy", profile_uid="p", conversation_uid="c", context_revision=1,
            facts=["旧事实"], hypotheses=["旧推测"], unknowns=["旧未知"]))
        dialog.show()
        self.wait_read(dialog)
        self.assertEqual(dialog.chooser.count(), 3)
        self.assertIn("旧事实", dialog.view.toPlainText())
        self.assertIn("引用不可用", dialog.view.toPlainText())
        self.assertTrue(all(row["citation_status"] == "unavailable" and not row["source_quotes"] for row in dialog.claims))

    def test_qt_history_pages_and_restore_create_new_pending_revision(self):
        self.gateway.request.side_effect = [dict(memory_uid="memo", profile_uid="p", versions=[record("memories", "memo", memory_payload(), 4)],
            has_more=True, next_before_version=4), dict(memory_uid="memo", profile_uid="p",
            versions=[record("memories", "memo", memory_payload("旧内容"), 2)], has_more=False, next_before_version=0)]
        dialog = self.ui.MemoryHistoryDialog(self.panel, "memo")
        dialog.show()
        self.wait_read(dialog)
        self.assertTrue(dialog.more_button.isEnabled())
        dialog.more_button.click()
        self.wait_read(dialog)
        self.assertEqual(dialog.chooser.count(), 2)
        self.assertFalse(dialog.more_button.isEnabled())
        self.assertEqual(self.gateway.request.call_args.args[1], "/v1/memories/memo/history?before_version=4&limit=50")
        dialog.chooser.setCurrentIndex(1)
        with patch.object(self.ui.QMessageBox, "question", return_value=self.ui.QMessageBox.StandardButton.Yes):
            dialog.restore_button.click()
        op = self.db.operations()[0]
        self.assertEqual((op["base_version"], op["payload"]["text"], op["payload"]["status"]), (4, "旧内容", "pending"))

    def test_qt_account_change_closes_clears_and_rejects_late_response_and_stale_editor_write(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        def delayed(*_args, **_kwargs):
            entered.set()
            release.wait(2)
            return dict(analysis_id="a", profile_uid="p", conversation_uid="c", context_revision=1, messages=[],
                        evidence=[dict(claim_id="f", kind="fact", text="上一账号私密内容", source_message_uids=[],
                                       source_quotes=[], citation_status="unavailable")])
        self.gateway.request.side_effect = delayed
        dialog = self.ui.EvidenceDialog(self.panel, dict(analysis_id="a", profile_uid="p", conversation_uid="c", context_revision=1))
        editor = self.ui.MemoryDialog(self.panel, "p")
        dialog.show()
        editor.show()
        self.assertTrue(entered.wait(1))
        self.service.identity = "changed-account"
        self.panel.refresh()
        release.set()
        self.wait_read(dialog)
        self.assertFalse(dialog.isVisible())
        self.assertFalse(editor.isVisible())
        self.assertNotIn("上一账号私密内容", dialog.view.toPlainText())
        self.assertEqual(editor.text.toPlainText(), "")
        editor.text.setPlainText("迟到编辑")
        editor.save_button.click()
        self.assertEqual(self.db.operations(), [])

    def test_qt_deleted_source_and_memory_clear_open_evidence_and_history(self):
        self.gateway.request.side_effect = [dict(analysis_id="a", profile_uid="p", conversation_uid="c", context_revision=1,
            messages=[dict(uid="m", version=1, role="her", text="周六见", observed_at="2026-10-21T01:00:00Z")],
            evidence=[dict(claim_id="f", kind="fact", text="约在周六", source_message_uids=["m"],
                           source_quotes=[dict(message_uid="m", quote="周六见")], citation_status="verified")]),
            dict(memory_uid="memo", profile_uid="p", versions=[record("memories", "memo", memory_payload(), 4)],
                 has_more=False, next_before_version=0)]
        evidence = self.ui.EvidenceDialog(self.panel, dict(analysis_id="a", profile_uid="p", conversation_uid="c", context_revision=1))
        evidence.show()
        self.wait_read(evidence)
        history = self.ui.MemoryHistoryDialog(self.panel, "memo")
        history.show()
        self.wait_read(history)
        self.db.apply_pull([dict(seq=10, record=record("messages", "m", {}, 2, "deleted")),
                            dict(seq=11, record=record("memories", "memo", {}, 5, "deleted"))], "deleted")
        evidence._check_lifetime()
        history._check_lifetime()
        self.assertFalse(evidence.isVisible())
        self.assertFalse(history.isVisible())
        self.assertEqual(evidence.view.toPlainText(), "")
        self.assertEqual(history.view.toPlainText(), "")

    def test_qt_history_404_after_first_page_clears_old_content_and_disables_restore(self):
        self.gateway.request.side_effect = [dict(memory_uid="memo", profile_uid="p", versions=[record("memories", "memo", memory_payload(), 4)],
            has_more=True, next_before_version=4), module("gateway").CloudError("memory_not_found", 404)]
        history = self.ui.MemoryHistoryDialog(self.panel, "memo")
        history.show()
        self.wait_read(history)
        self.assertIn("原事实", history.view.toPlainText())
        history.more_button.click()
        self.wait_read(history)
        self.assertEqual(history.view.toPlainText(), "")
        self.assertFalse(history.restore_button.isEnabled())

    def test_qt_fact_edit_requires_reason_and_remains_pending_until_separate_confirmation(self):
        dialog = self.ui.MemoryDialog(self.panel, "p")
        dialog.show()
        dialog.chooser.setCurrentIndex(dialog.chooser.findData("memo"))
        dialog.text.setPlainText("更正的事实")
        self.assertFalse(dialog.confirm_button.isEnabled())
        dialog.reason.clear()
        dialog.save_button.click()
        self.assertEqual(self.db.operations(), [])
        dialog.reason.setText("核对后修正")
        dialog.save_button.click()
        self.assertEqual(self.db.visible("memories", "memo")["payload"]["status"], "pending")
        self.assertEqual(self.db.visible("memories", "memo")["payload"]["change_reason"], "核对后修正")
        self.assertFalse(dialog.confirm_button.isEnabled())
        self.assertIn("先同步", dialog.status.text())
        correction = self.db.operations()[0]
        self.db.apply_push([dict(op_id=correction["op_id"], status="applied",
                                record=record("memories", "memo", correction["payload"], version=5))])
        dialog.refresh("memo")
        self.assertTrue(dialog.confirm_button.isEnabled())
        with patch.object(self.ui.QMessageBox, "question", return_value=self.ui.QMessageBox.StandardButton.Yes):
            dialog.confirm_button.click()
        confirmation = self.db.operations()[0]
        self.assertEqual(confirmation["base_version"], 5)
        self.assertEqual(confirmation["payload"]["status"], "confirmed")



if __name__ == "__main__":
    unittest.main()
