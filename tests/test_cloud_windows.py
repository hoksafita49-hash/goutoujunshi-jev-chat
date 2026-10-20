"""Windows cloud contract and persistence checks; no OS capture or paid calls."""
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock


def module(name):
    try:
        return importlib.import_module("integrations.jev_windows.app." + name)
    except ModuleNotFoundError:
        return None


def record(kind, uid, payload, version=1, deleted_at=None):
    return dict(entity_type=kind, uid=uid, version=version, payload=payload,
                created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
                deleted_at=deleted_at)


class CloudWindowsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def db(self, user="one"):
        mod = module("local_db")
        self.assertIsNotNone(mod, "Windows needs a durable per-account repository")
        db = mod.LocalDatabase(self.root, "https://api.example.test", user)
        self.addCleanup(db.close)
        return db

    def test_atomic_edit_outbox_survives_restart_and_is_account_scoped(self):
        db = self.db()
        db.save("profiles", "p", {"name": "甲"})
        self.assertEqual(db.visible("profiles", "p")["payload"]["name"], "甲")
        self.assertEqual(len(db.operations()), 1)
        self.assertIsNone(self.db("two").visible("profiles", "p"))
        self.assertEqual(self.db().operations(), db.operations())

    def test_ambiguous_push_reuses_immutable_operation_and_keeps_later_edit(self):
        db = self.db()
        db.save("profiles", "p", {"name": "第一稿"})
        first = db.operations()[0]
        db.mark_attempted([first["op_id"]])
        db.save("profiles", "p", {"name": "第二稿"})
        self.assertEqual(db.operations()[0], first)
        db.apply_push([dict(op_id=first["op_id"], status="applied",
                            record=record("profiles", "p", first["payload"]))])
        next_op = db.operations()[0]
        self.assertNotEqual(first["op_id"], next_op["op_id"])
        self.assertEqual(next_op["base_version"], 1)
        self.assertEqual(next_op["payload"]["name"], "第二稿")

    def test_pull_keeps_dirty_content_and_both_conflict_versions(self):
        db = self.db()
        db.apply_pull([dict(seq=1, record=record("profiles", "p", {"name": "原稿"}))], "a")
        db.save("profiles", "p", {"name": "本地"})
        db.apply_pull([dict(seq=2, record=record("profiles", "p", {"name": "远端"}, 2))], "b")
        self.assertEqual(db.cursor, "b")
        self.assertEqual(db.visible("profiles", "p")["payload"]["name"], "本地")
        conflict = db.conflicts()[0]
        self.assertEqual(conflict["local"]["name"], "本地")
        self.assertEqual(conflict["remote"]["payload"]["name"], "远端")
        db.resolve(conflict["id"], "local")
        self.assertEqual(db.operations()[0]["base_version"], 2)

    def test_push_receipt_never_advances_pull_cursor(self):
        db = self.db()
        db.save("profiles", "p", {"name": "甲"})
        op = db.operations()[0]
        db.apply_push([dict(op_id=op["op_id"], status="applied", seq=99,
                            record=record("profiles", "p", op["payload"]))])
        self.assertEqual(db.cursor, "")
        self.assertEqual(db.operations(), [])

    def test_epoch_change_freezes_queue_and_creates_recovery_copy(self):
        db = self.db()
        db.check_epoch("old")
        db.save("profiles", "p", {"name": "保留"})
        with self.assertRaisesRegex(RuntimeError, "server_epoch_changed"):
            db.check_epoch("new")
        self.assertTrue(db.frozen)
        self.assertEqual(len(list(self.root.glob("**/*.recovery-*.sqlite3"))), 1)
        self.assertEqual(len(db.operations()), 1)

    def test_explicit_legacy_import_is_stable_and_preserves_source(self):
        mod = module("legacy_import")
        self.assertIsNotNone(mod, "Migration must be explicitly previewed")
        source = self.root / "config.json"
        original = json.dumps({"profiles": {"同名": {"stage": "初识"}},
                               "app_profiles": {"qq": {"同名": {"background": "QQ"}}}}, ensure_ascii=False)
        source.write_text(original, encoding="utf-8")
        db = self.db()
        preview = mod.preview_profiles(source)
        self.assertEqual(len(preview), 2)
        self.assertEqual(db.operations(), [])
        mod.import_profiles(db, preview, "device")
        uids = [r["uid"] for r in db.list_records("profiles")]
        mod.import_profiles(db, preview, "device")
        self.assertEqual([r["uid"] for r in db.list_records("profiles")], uids)
        self.assertEqual(source.read_text(encoding="utf-8"), original)

    def test_confirmed_repeated_short_messages_are_not_globally_deduped(self):
        db = self.db()
        db.confirm_messages("c", "p", [("her", "好"), ("her", "好")])
        messages = db.list_records("messages")
        self.assertEqual(len(messages), 2)
        self.assertNotEqual(messages[0]["uid"], messages[1]["uid"])
        db.confirm_messages("c", "p", [("her", "好"), ("her", "好")])
        self.assertEqual(len(db.list_records("messages")), 2)

    def test_job_request_id_persisted_before_submission_and_reused(self):
        db = self.db()
        first = db.prepare_job("c", 3, "analysis")
        second = self.db().prepare_job("c", 3, "analysis")
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertEqual(first["body"], second["body"])

    def test_null_weights_and_server_candidate_order_survive_ui_mapping(self):
        mod = module("gateway")
        self.assertIsNotNone(mod, "Cloud results need a faithful view mapping")
        result = dict(analysis_id="a", conversation_uid="c", context_revision=2,
                      ranking_status="unavailable", facts=["事实"], unknowns=["未知"],
                      candidates=[dict(candidate_id="second", text="B", weight=None),
                                  dict(candidate_id="first", text="A", weight=None)])
        mapped = mod.result_for_ui(result)
        self.assertEqual(mapped["candidates"], ["B", "A"])
        self.assertEqual(mapped["candidate_ids"], ["second", "first"])
        self.assertEqual(mapped["scores"], [None, None])
        self.assertTrue(mapped["server_order"])

    def test_gateway_rejects_unsafe_origins_and_auth_uses_three_headers(self):
        mod = module("gateway")
        self.assertIsNotNone(mod, "Client must enforce gateway transport rules")
        for url in ("http://example.test", "https://user:secret@example.test", "https://x.test?key=x",
                    "http://127.0.0.1:9999", "https://x.test/#fragment"):
            with self.assertRaises(ValueError):
                mod.GatewayClient(url)
        client = mod.GatewayClient("http://127.0.0.1:18000", dict(token="t", device=dict(id="d"), device_secret="s"))
        self.assertEqual(client.auth_headers(), {"Authorization": "Bearer t", "X-Device-Session": "d", "X-Device-Secret": "s"})

    def test_session_storage_contains_protected_bytes_only(self):
        mod = module("auth_store")
        self.assertIsNotNone(mod, "Sessions need protected OS storage")
        vault = mod.SessionStore(self.root, protect=lambda raw: b"protected:" + raw[::-1],
                                 unprotect=lambda raw: raw[len(b"protected:"):][::-1])
        vault.save("https://api.example.test", dict(token="private-token", device_secret="secret"))
        data = next(self.root.glob("*.bin")).read_bytes()
        self.assertNotIn(b"private-token", data)
        self.assertEqual(vault.load("https://api.example.test")["token"], "private-token")
        vault.clear("https://api.example.test")
        self.assertIsNone(vault.load("https://api.example.test"))

    def test_sync_does_not_upload_after_epoch_change_and_uses_pagination(self):
        mod = module("sync_worker")
        self.assertIsNotNone(mod, "Foreground sync coordinator is required")
        db = self.db()
        db.check_epoch("one")
        gateway = Mock()
        gateway.request.side_effect = [dict(server_epoch="one"),
                                       dict(server_epoch="one", changes=[], cursor="page1", has_more=True),
                                       dict(server_epoch="one", changes=[], cursor="page2", has_more=False)]
        mod.SyncCoordinator(db, gateway).sync()
        self.assertEqual(db.cursor, "page2")
        gateway.request.reset_mock()
        gateway.request.side_effect = None
        gateway.request.return_value = dict(server_epoch="restored")
        db.save("profiles", "p", {"name": "离线"})
        with self.assertRaisesRegex(RuntimeError, "server_epoch_changed"):
            mod.SyncCoordinator(db, gateway).sync()
        self.assertEqual(gateway.request.call_count, 1)

    def test_job_submit_ambiguity_keeps_same_request_and_never_falls_back(self):
        mod = module("repositories")
        self.assertIsNotNone(mod, "A cloud-only analysis path is required")
        db = self.db()
        gateway = Mock()
        gateway.request.side_effect = RuntimeError("network_ambiguous")
        service = mod.CloudRepository(db, gateway)
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "network_ambiguous"):
                service.run_job("c", 2)
        calls = gateway.request.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].args[2]["request_id"], calls[1].args[2]["request_id"])

    def test_job_result_must_match_conversation_and_revision(self):
        mod = module("repositories")
        self.assertIsNotNone(mod)
        gateway = Mock()
        gateway.request.side_effect = [dict(analysis_id="a", status="queued"),
                                       dict(analysis_id="a", status="succeeded", conversation_uid="wrong",
                                            context_revision=2, result={})]
        with self.assertRaisesRegex(RuntimeError, "context_mismatch"):
            mod.CloudRepository(self.db(), gateway).run_job("c", 2)

    def test_runtime_401_clears_session_but_preserves_account_drafts(self):
        runtime = module("cloud_runtime")
        self.assertIsNotNone(runtime, "Account state must be invalidated on 401")
        auth = module("auth_store")
        vault = auth.SessionStore(self.root / "vault", protect=lambda x: x[::-1], unprotect=lambda x: x[::-1])
        session = dict(token="t", device=dict(id="d"), device_secret="s", user=dict(id="one", email="one@test"), server_epoch="epoch")
        vault.save("https://api.example.test", session)
        service = runtime.CloudSession("https://api.example.test", self.root / "cache", vault=vault)
        self.addCleanup(service.close)
        db = service.repository.db
        db.save("profiles", "p", {"name": "离线草稿"})
        service.gateway.request = Mock(side_effect=module("gateway").CloudError("device_revoked", 401))
        with self.assertRaises(RuntimeError):
            service.sync()
        self.assertIsNone(service.session)
        self.assertIsNone(vault.load(service.server))
        self.assertEqual(len(db.operations()), 1)

    def test_order_of_confirmed_rows_is_preserved_even_when_observed_together(self):
        db = self.db()
        db.confirm_messages("c", "p", [("me", "先"), ("her", "后")])
        rows = sorted(db.list_records("messages"), key=lambda r: r["payload"]["order_key"])
        self.assertEqual([r["payload"]["text"] for r in rows], ["先", "后"])

    def test_sliding_confirmed_window_preserves_older_messages(self):
        db = self.db()
        db.confirm_messages("c", "p", [("me", "早"), ("her", "中")])
        db.confirm_messages("c", "p", [("her", "中"), ("me", "晚")])
        rows = db.list_records("messages")
        self.assertEqual(sorted(r["payload"]["text"] for r in rows), sorted(["早", "中", "晚"]))

    def test_cloud_analysis_never_calls_local_model_or_vendor_keys(self):
        from unittest.mock import patch
        from test_windows_capture_flow import load_main
        main = load_main()
        main.settings.cloud_mode.return_value = True
        service = Mock()
        service.identity = "account-generation"
        service.analyze.return_value = dict(candidates=["云候选"], analysis_id="a")
        with patch.dict("sys.modules", {"app.cloud_runtime": Mock(current=Mock(return_value=service))}):
            main.analyze_bg([("her", "你好")], "我是好人", 0, epoch=0)
        main.analyze.assert_not_called()
        main.settings.strategy_key.assert_not_called()
        self.assertEqual(main.results.get()[1]["cloud_identity"], "account-generation")

    def test_backfill_order_key_places_older_capture_before_existing_rows(self):
        db = self.db()
        db.confirm_messages("c", "p", [("her", "中"), ("me", "晚")])
        db.confirm_messages("c", "p", [("me", "早"), ("her", "中"), ("me", "晚")])
        rows = sorted(db.list_records("messages"), key=lambda r: r["payload"]["order_key"])
        self.assertEqual([r["payload"]["text"] for r in rows], ["早", "中", "晚"])

    def test_result_visibility_rejects_deleted_or_changed_context(self):
        runtime = module("cloud_runtime")
        auth = module("auth_store")
        vault = auth.SessionStore(self.root / "vault", protect=lambda x: x[::-1], unprotect=lambda x: x[::-1])
        vault.save("https://api.example.test", dict(token="t", device=dict(id="d"), device_secret="s", user=dict(id="one")))
        service = runtime.CloudSession("https://api.example.test", self.root, vault=vault)
        self.addCleanup(service.close)
        service.repository.db.apply_pull([dict(seq=1, record=record("profiles", "p", {"name": "甲"})),
                                          dict(seq=2, record=record("conversations", "c", {"profile_uid": "p", "context_revision": 4}))], "a")
        self.assertFalse(service.accept_result(dict(cloud_identity=service.identity, profile_uid="p", conversation_uid="c", context_revision=3)))
        self.assertTrue(service.accept_result(dict(cloud_identity=service.identity, profile_uid="p", conversation_uid="c", context_revision=4)))

    def test_dpapi_roundtrip_for_synthetic_session(self):
        import os
        if os.name != "nt":
            self.skipTest("DPAPI is Windows only")
        vault = module("auth_store").SessionStore(self.root / "dpapi")
        vault.save("https://fake.example.test", {"token": "synthetic-test-only"})
        self.assertEqual(vault.load("https://fake.example.test"), {"token": "synthetic-test-only"})

    def test_restore_requires_explicit_adoption_and_keeps_original_queue_in_copy(self):
        import sqlite3
        from contextlib import closing
        db = self.db()
        db.check_epoch("old")
        db.save("profiles", "p", {"name": "旧备份外的草稿"})
        with self.assertRaises(RuntimeError):
            db.check_epoch("new")
        self.assertTrue(hasattr(db, "adopt_epoch_after_review"), "Restore recovery needs an explicit user action")
        db.adopt_epoch_after_review("new")
        self.assertEqual(db.epoch, "new")
        self.assertFalse(db.frozen)
        self.assertEqual(db.operations(), [])
        recovery = next(self.root.glob("**/*.recovery-*.sqlite3"))
        with closing(sqlite3.connect(recovery)) as copy:
            self.assertEqual(copy.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)

    def test_cloud_style_is_scoped_to_account_and_legacy_settings_are_preserved(self):
        from test_windows_capture_flow import settings
        from unittest.mock import patch
        runtime = module("cloud_runtime")
        db = self.db()
        service = Mock()
        service.require.return_value.db = db
        config = self.root / "config.json"
        config.write_text(json.dumps(dict(connection_mode="cloud", cloud_server="https://api.example.test", style="原直连偏好")), encoding="utf-8")
        spec = importlib.util.spec_from_loader("integrations.jev_windows.app.settings", loader=None)
        with patch.object(settings, "_CONFIG", str(config)), patch.object(settings, "__spec__", spec), \
                patch.object(settings, "__package__", "integrations.jev_windows.app"), \
                patch.object(runtime, "current", return_value=service):
            self.assertEqual(settings.style(), "")
            settings.save(style_text="云账号风格")
            self.assertEqual(settings.style(), "云账号风格")
            settings.save(debug_view_on=False)
        stored = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual(stored["cloud_server"], "https://api.example.test")
        self.assertEqual(stored["style"], "原直连偏好")
        self.assertEqual(self.db("two").list_records("preferences"), [])

    def test_new_paid_request_requires_explicit_new_attempt(self):
        db = self.db()
        first = db.prepare_job("c", 1, "analysis")
        self.assertEqual(db.prepare_job("c", 1, "analysis")["request_id"], first["request_id"])
        self.assertNotEqual(db.prepare_job("c", 1, "analysis", new_request=True)["request_id"], first["request_id"])

    def test_common_result_and_sync_fixtures(self):
        fixture_root = Path(__file__).resolve().parents[1] / "contracts/fixtures"
        result = json.loads((fixture_root / "cloud-v1-result.json").read_text(encoding="utf-8"))
        mapped = module("gateway").result_for_ui(result)
        self.assertEqual(mapped["candidate_ids"], [r["candidate_id"] for r in result["candidates"]])
        self.assertEqual(mapped["scores"], [None, None])
        page = json.loads((fixture_root / "cloud-v1-sync-page.json").read_text(encoding="utf-8"))
        db = self.db()
        db.check_epoch(page["server_epoch"])
        db.apply_pull(page["changes"], page["cursor"])
        self.assertEqual(db.cursor, "opaque-test-cursor")
        self.assertEqual(len(db.list_records("profiles")), 1)
        self.assertEqual(db.list_records("notes"), [])

    def test_analysis_captured_before_account_change_cannot_upload(self):
        runtime = module("cloud_runtime")
        auth = module("auth_store")
        vault = auth.SessionStore(self.root / "vault", protect=lambda x: x[::-1], unprotect=lambda x: x[::-1])
        vault.save("https://api.example.test", dict(token="t", device=dict(id="d"), device_secret="s", user=dict(id="one")))
        service = runtime.CloudSession("https://api.example.test", self.root / "cache", vault=vault)
        self.addCleanup(service.close)
        service.gateway.request = Mock()
        with self.assertRaisesRegex(RuntimeError, "account_changed"):
            service.analyze("wechat", "旧会话", [("her", "旧账号原文")], {}, expected_identity="old-account")
        service.gateway.request.assert_not_called()
        self.assertEqual(service.repository.db.operations(), [])

    def test_explicit_review_deletion_is_distinct_from_offscreen_history(self):
        db = self.db()
        source = [("her", "删掉"), ("me", "保留")]
        db.confirm_messages("c", "p", source)
        db.confirm_messages("c", "p", [("me", "保留")], source_messages=source)
        self.assertEqual([r["payload"]["text"] for r in db.list_records("messages")], ["保留"])
        self.assertTrue(all(op["action"] == "upsert" for op in db.operations()), "Never send a tombstone for a never-uploaded message")

    def test_switching_to_direct_before_cloud_worker_runs_never_uses_vendor_key(self):
        from test_windows_capture_flow import load_main
        main = load_main()
        main.settings.cloud_mode.return_value = False
        main.analyze_bg([("her", "已核对")], "我是好人", 0, epoch=0, account_identity="cloud-account")
        main.analyze.assert_not_called()
        main.settings.strategy_key.assert_not_called()
        self.assertEqual(main.results.get()[0], "err")

    def test_server_requires_newer_client_before_any_upload(self):
        db = self.db()
        db.save("profiles", "p", {"name": "离线"})
        gateway = Mock()
        gateway.request.return_value = dict(server_epoch="e", api_version=2, min_client_version=2)
        with self.assertRaisesRegex(RuntimeError, "client_upgrade_required"):
            module("sync_worker").SyncCoordinator(db, gateway).sync()
        self.assertEqual(gateway.request.call_count, 1)

    def test_historical_pull_after_newer_receipt_does_not_conflict_with_new_edit(self):
        db = self.db()
        db.save("profiles", "p", {"name": "已上传"})
        op = db.operations()[0]
        db.apply_push([dict(op_id=op["op_id"], status="applied", record=record("profiles", "p", {"name": "远端v3"}, 3))])
        self.assertEqual(db.cursor, "")
        db.save("profiles", "p", {"name": "基于v3的新草稿"})
        db.apply_pull([dict(seq=v, record=record("profiles", "p", {"name": "远端v" + str(v)}, v)) for v in (1, 2, 3)], "through-v3")
        self.assertEqual(db.conflicts(), [])
        self.assertEqual(db.operations()[0]["base_version"], 3)
        self.assertEqual(db.visible("profiles", "p")["payload"]["name"], "基于v3的新草稿")
        self.assertEqual(db.cursor, "through-v3")

    def test_old_pull_change_cannot_replace_newer_remote_conflict_copy(self):
        db = self.db()
        db.apply_pull([dict(seq=1, record=record("profiles", "p", {"name": "v1"}))], "one")
        db.save("profiles", "p", {"name": "local"})
        db.apply_pull([dict(seq=4, record=record("profiles", "p", {"name": "v4"}, 4))], "four")
        db.apply_pull([dict(seq=2, record=record("profiles", "p", {"name": "v2"}, 2))], "older")
        self.assertEqual(db.conflicts()[0]["remote"]["version"], 4)

    def test_editing_unsent_parent_keeps_it_before_dependent_operations(self):
        db = self.db()
        db.save("profiles", "p", {"name": "初稿"})
        db.save("conversations", "c", {"profile_uid": "p"})
        db.save("messages", "m", {"profile_uid": "p", "conversation_uid": "c", "text": "你好"})
        db.save("profiles", "p", {"name": "改稿"})
        self.assertEqual([op["entity_type"] for op in db.operations()], ["profiles", "conversations", "messages"])
        self.assertEqual(db.operations()[0]["payload"]["name"], "改稿")

    def test_editor_open_before_remote_update_cannot_silently_overwrite_new_version(self):
        db = self.db()
        db.apply_pull([dict(seq=1, record=record("profiles", "p", {"name": "原稿"}))], "one")
        version_seen = db.visible("profiles", "p")["version"]
        db.apply_pull([dict(seq=2, record=record("profiles", "p", {"name": "另一端修改"}, 2))], "two")
        db.save("profiles", "p", {"name": "编辑器旧快照的修改"}, expected_version=version_seen)
        self.assertEqual(db.operations(), [])
        self.assertEqual(db.conflicts()[0]["remote"]["version"], 2)
        self.assertEqual(db.conflicts()[0]["local"]["name"], "编辑器旧快照的修改")

    def test_browser_resume_reuses_existing_request_with_its_original_preferences(self):
        db = self.db()
        original = db.prepare_job("c", 2, "analysis", preferences={"style": "原始偏好"})
        gateway = Mock()
        gateway.request.side_effect = RuntimeError("network_ambiguous")
        with self.assertRaises(RuntimeError):
            module("repositories").CloudRepository(db, gateway).run_job("c", 2)
        sent = gateway.request.call_args.args[2]
        self.assertEqual(sent["request_id"], original["request_id"])
        self.assertEqual(sent["preferences"], {"style": "原始偏好"})

    def test_rebinding_person_after_review_cannot_move_confirmed_text_to_new_person(self):
        runtime, auth = module("cloud_runtime"), module("auth_store")
        vault = auth.SessionStore(self.root / "vault", protect=lambda x: x[::-1], unprotect=lambda x: x[::-1])
        vault.save("https://api.example.test", dict(token="t", device=dict(id="d"), device_secret="s", user=dict(id="one")))
        service = runtime.CloudSession("https://api.example.test", self.root / "cache", vault=vault)
        self.addCleanup(service.close)
        repo = service.require()
        repo.db.save("profiles", "p", {"name": "原人物"})
        repo.db.save("profiles", "q", {"name": "另一个人"})
        conv = repo.bind_chat("wechat", "聊天", "p", service.source_device)
        repo.bind_chat("wechat", "聊天", "q", service.source_device)
        service.gateway.request = Mock()
        with self.assertRaisesRegex(RuntimeError, "context_changed"):
            service.analyze("wechat", "聊天", [("her", "原人物的文本")], {}, expected_identity=service.identity,
                            expected_context=("p", conv))
        self.assertEqual(repo.db.list_records("messages"), [])
        service.gateway.request.assert_not_called()

    def test_rejected_drafts_only_get_new_operation_ids_after_explicit_retry(self):
        db = self.db()
        db.save("profiles", "p", {"name": "待修复"})
        original = db.operations()[0]
        db.mark_attempted([original["op_id"]])
        db.apply_push([dict(op_id=original["op_id"], status="rejected", code="dependency_failed")])
        self.assertEqual(db.operations(), [])
        self.assertEqual(db.visible("profiles", "p")["payload"]["name"], "待修复")
        self.assertEqual(db.retry_rejected(), 1)
        self.assertNotEqual(db.operations()[0]["op_id"], original["op_id"])
        self.assertEqual(db.operations()[0]["payload"], original["payload"])


if __name__ == "__main__":
    unittest.main()
