"""Real PocketBase tests, isolated database; no provider calls or production credentials."""
import concurrent.futures
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import unittest
import uuid

import httpx

ROOT = Path(__file__).resolve().parents[3]
INTERNAL = "test-internal-" + "x" * 48
PASSWORD = "Only-for-isolated-tests-123!"


def uid():
    return str(uuid.uuid4())


class PocketBaseSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        binary = Path(os.environ.get("PB_BINARY", ROOT.parent / "pocketbase-test-runtime/pocketbase.exe"))
        if not binary.is_file():
            raise unittest.SkipTest("PB_BINARY must point to pinned PocketBase 0.40.5")
        cls.temp = tempfile.TemporaryDirectory(prefix="goutou-pb-test-")
        cls.folder = Path(cls.temp.name)
        cls.hooks = cls.folder / "hooks"
        cls.migrations = cls.folder / "migrations"
        for source, target in ((ROOT / "services/pocketbase/pb_hooks", cls.hooks),
                               (ROOT / "services/pocketbase/pb_migrations", cls.migrations)):
            if source.exists():
                shutil.copytree(source, target)
            else:
                target.mkdir()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        cls.url = f"http://127.0.0.1:{port}"
        cls.log = open(cls.folder / "server.log", "wb")
        environment = os.environ.copy()
        environment["GOUTOU_PB_INTERNAL_KEY"] = INTERNAL
        cls.process = subprocess.Popen([str(binary), "serve", f"--http=127.0.0.1:{port}",
            "--dir=" + str(cls.folder / "data"), "--hooksDir=" + str(cls.hooks),
            "--migrationsDir=" + str(cls.migrations), "--hooksWatch=false", "--automigrate=false"],
            env=environment, stdout=cls.log, stderr=cls.log)
        cls.client = httpx.Client(base_url=cls.url, trust_env=False, timeout=10)
        for _ in range(60):
            try:
                if cls.client.get("/api/health").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if cls.process.poll() is not None:
                cls.log.flush()
                raise AssertionError((cls.folder / "server.log").read_text(errors="replace")[-5000:])
            time.sleep(0.1)
        else:
            raise AssertionError("isolated PocketBase failed to start")

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.process.terminate()
        cls.process.wait(timeout=10)
        cls.log.close()
        cls.temp.cleanup()

    def call(self, action, body=None, session=None):
        if action == "job-claim" and body is None:
            body = {"claim_token": uuid.uuid4().hex + uuid.uuid4().hex}
        headers = {"X-Internal-Key": INTERNAL}
        if session:
            headers.update({"X-Account-Token": session["token"],
                            "X-Device-Session": session["device"]["id"],
                            "X-Device-Secret": session["device_secret"]})
        return self.client.post("/internal/cloud/" + action, json=body or {}, headers=headers)

    def account(self):
        email = uid() + "@example.test"
        response = self.call("provision", {"email": email, "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text[:500])
        return email

    def login(self, email, name="test Windows"):
        response = self.call("login", {"email": email, "password": PASSWORD,
            "device_name": name, "platform": "windows"})
        self.assertEqual(response.status_code, 200, response.text[:500])
        return response.json()

    def setUp(self):
        self.email = self.account()
        self.session = self.login(self.email)
        self.epoch = self.session["server_epoch"]

    def push(self, operations, session=None):
        response = self.call("push", {"server_epoch": self.epoch, "operations": operations}, session or self.session)
        self.assertEqual(response.status_code, 200, response.text[:500])
        return response.json()["results"]

    def profile(self, name="Test profile"):
        operation = {"op_id": uid(), "entity_type": "profiles", "entity_uid": uid(),
                     "base_version": 0, "action": "upsert", "payload": {"name": name}}
        return operation

    def test_internal_routes_require_independent_service_key(self):
        response = self.client.post("/internal/cloud/provision", json={"email": "x@example.test", "password": PASSWORD})
        self.assertEqual(response.status_code, 403)

    def test_account_provision_replay_does_not_reset_existing_password(self):
        same = self.call("provision", {"email": self.email, "password": PASSWORD})
        self.assertEqual(same.status_code, 200)
        self.assertEqual(same.json()["id"], self.session["user"]["id"])
        different = self.call("provision", {"email": self.email, "password": "Different-test-password!"})
        self.assertEqual(different.status_code, 409)
        self.assertEqual(self.call("auth", session=self.session).status_code, 200)

    def test_login_revoke_and_refresh_require_active_device(self):
        self.assertEqual(self.call("auth", session=self.session).status_code, 200)
        self.assertEqual(self.call("logout", session=self.session).status_code, 200)
        self.assertEqual(self.call("auth", session=self.session).status_code, 401)
        self.assertEqual(self.call("refresh", session=self.session).status_code, 401)

    def test_op_replay_and_different_body_reuse(self):
        operation = self.profile()
        first = self.push([operation])[0]
        replay = self.push([operation])[0]
        self.assertEqual(first, replay)
        self.assertEqual(first["status"], "applied")
        changed = dict(operation, payload={"name": "Changed"})
        self.assertEqual(self.push([changed])[0]["code"], "op_id_reused")
        changes = self.call("pull", session=self.session).json()["changes"]
        self.assertEqual(len(changes), 1)

    def test_two_device_conflict_preserves_both_versions(self):
        operation = self.profile("First")
        self.push([operation])
        second = self.login(self.email, "Android")
        update = dict(operation, op_id=uid(), base_version=1, payload={"name": "Second"})
        self.assertEqual(self.push([update], second)[0]["status"], "applied")
        stale = dict(update, op_id=uid(), payload={"name": "Offline edit"})
        conflict = self.push([stale])[0]
        self.assertEqual(conflict["status"], "conflict")
        self.assertEqual(conflict["record"]["payload"]["name"], "Second")
        self.assertEqual(conflict["record"]["version"], 2)

    def test_failed_operation_cannot_be_bypassed_by_later_same_entity_in_batch(self):
        operation = self.profile("Original")
        self.push([operation])
        conflict = dict(operation, op_id=uid(), payload={"name": "Stale"})
        bypass = dict(operation, op_id=uid(), base_version=1, payload={"name": "Must not bypass"})
        results = self.push([conflict, bypass])
        self.assertEqual(results[0]["status"], "conflict")
        self.assertEqual(results[1]["status"], "rejected")
        self.assertEqual(results[1]["code"], "dependency_failed")

    def test_receipt_failure_rolls_back_entity_and_change(self):
        # Inject a DB constraint fault only in this disposable PB database.
        path = self.folder / "data/data.db"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TRIGGER cloud_test_receipt_failure BEFORE INSERT ON cloud_receipts BEGIN SELECT RAISE(ABORT, 'isolated receipt fault'); END")
            connection.commit()
        operation = self.profile()
        try:
            response = self.call("push", {"server_epoch": self.epoch, "operations": [operation]}, self.session)
            self.assertEqual(response.status_code, 500)
            self.assertEqual(self.call("pull", session=self.session).json()["changes"], [])
        finally:
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("DROP TRIGGER cloud_test_receipt_failure")
                connection.commit()
        self.assertEqual(self.push([operation])[0]["status"], "applied")

    def test_foreign_parent_and_owner_are_rejected(self):
        operation = self.profile()
        self.push([operation])
        foreign = self.login(self.account())
        conversation = {"op_id": uid(), "entity_type": "conversations", "entity_uid": uid(),
            "base_version": 0, "action": "upsert", "payload": {"profile_uid": operation["entity_uid"], "title": "Foreign"}}
        self.assertEqual(self.push([conversation], foreign)[0]["status"], "rejected")
        malicious = dict(self.profile(), owner=self.session["user"]["id"])
        self.assertEqual(self.push([malicious], foreign)[0]["status"], "rejected")
        self.assertEqual(self.call("pull", session=foreign).json()["changes"], [])

    def test_pagination_freezes_bound_and_later_scan_finds_new_changes(self):
        self.push([self.profile("A"), self.profile("B")])
        first = self.call("pull", {"limit": 1}, self.session).json()
        self.assertTrue(first["has_more"])
        self.push([self.profile("C")])
        second = self.call("pull", {"cursor": first["cursor"], "limit": 10}, self.session).json()
        self.assertFalse(second["has_more"])
        self.assertEqual(len(second["changes"]), 1)
        third = self.call("pull", {"cursor": second["cursor"], "limit": 10}, self.session).json()
        self.assertEqual(len(third["changes"]), 1)

    def test_deletion_scrubs_history_and_cannot_be_resurrected(self):
        operation = self.profile("Sensitive historical note")
        self.push([operation])
        delete = dict(operation, op_id=uid(), action="delete", base_version=1, payload={})
        self.assertEqual(self.push([delete])[0]["status"], "applied")
        history = self.call("pull", session=self.session).json()
        self.assertNotIn("Sensitive historical note", json.dumps(history))
        resurrect = dict(operation, op_id=uid(), base_version=2)
        self.assertEqual(self.push([resurrect])[0]["code"], "entity_deleted")

    def test_concurrent_creates_have_one_winner_and_one_change(self):
        first = self.profile()
        second = dict(first, op_id=uid(), payload={"name": "Concurrent"})
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda op: self.push([op])[0], [first, second]))
        self.assertEqual(sorted(item["status"] for item in results), ["applied", "conflict"])
        self.assertEqual(len(self.call("pull", session=self.session).json()["changes"]), 1)

    def conversation(self):
        profile = self.profile()
        self.push([profile])
        conversation = {"op_id": uid(), "entity_type": "conversations", "entity_uid": uid(),
            "base_version": 0, "action": "upsert", "payload": {"profile_uid": profile["entity_uid"], "title": "Test"}}
        self.push([conversation])
        message = {"op_id": uid(), "entity_type": "messages", "entity_uid": uid(),
            "base_version": 0, "action": "upsert", "payload": {"profile_uid": profile["entity_uid"],
            "conversation_uid": conversation["entity_uid"], "text": "Confirmed test only", "role": "other", "confirmed": True,
            "observed_at": "2026-10-21T00:00:00Z", "order_key": "000001"}}
        self.push([message])
        return conversation["entity_uid"]

    def test_jobs_are_idempotent_and_finish_once_into_sync_log(self):
        conversation = self.conversation()
        request = {"request_id": uid(), "conversation_uid": conversation, "context_revision": 1, "kind": "analysis"}
        created = self.call("job-create", request, self.session)
        self.assertEqual(created.status_code, 202, created.text)
        job = created.json()
        self.assertEqual(self.call("job-create", request, self.session).json(), job)
        changed = dict(request, preferences={"tone": "different"})
        self.assertEqual(self.call("job-create", changed, self.session).status_code, 409)
        claim = self.call("job-claim").json()
        self.assertEqual(claim["job"]["analysis_id"], job["analysis_id"])
        self.assertEqual(claim["job"]["input"]["messages"][0]["text"], "Confirmed test only")
        result = {"schema_version": 1, "analysis_id": job["analysis_id"], "conversation_uid": conversation,
                  "context_revision": 1, "candidates": [], "ranking_status": "unavailable"}
        finish = {"analysis_id": job["analysis_id"], "claim_token": claim["claim_token"], "status": "succeeded", "result": result}
        self.assertEqual(self.call("job-finish", finish).status_code, 200)
        self.assertEqual(self.call("job-finish", finish).status_code, 200)
        saved = self.call("job-get", {"id": job["analysis_id"]}, self.session).json()
        self.assertEqual(saved["status"], "succeeded")
        self.assertEqual(saved["result"], result)
        changes = self.call("pull", session=self.session).json()["changes"]
        self.assertEqual(len([c for c in changes if c["record"]["entity_type"] == "analyses"
                              and c["record"]["payload"]["status"] == "succeeded"]), 1)

    def test_job_stale_revision_is_rejected_and_running_recovery_is_indeterminate(self):
        conversation = self.conversation()
        request = {"request_id": uid(), "conversation_uid": conversation, "context_revision": 0, "kind": "analysis"}
        self.assertEqual(self.call("job-create", request, self.session).status_code, 409)
        request["context_revision"] = 1
        job = self.call("job-create", request, self.session).json()
        self.call("job-claim")
        self.assertEqual(self.call("job-recover").status_code, 200)
        saved = self.call("job-get", {"id": job["analysis_id"]}, self.session).json()
        self.assertEqual(saved["status"], "indeterminate")

    def test_claim_response_replay_returns_same_job_without_claiming_the_next_one(self):
        conversation = self.conversation()
        self.addCleanup(lambda: self.push([{"op_id": uid(), "entity_type": "conversations", "entity_uid": conversation,
            "base_version": 2, "action": "delete", "payload": {}}]))
        first = self.call("job-create", {"request_id": uid(), "conversation_uid": conversation,
            "context_revision": 1, "kind": "analysis"}, self.session).json()
        second = self.call("job-create", {"request_id": uid(), "conversation_uid": conversation,
            "context_revision": 1, "kind": "analysis"}, self.session).json()
        token = "replay-token-" + "x" * 52
        claim = self.call("job-claim", {"claim_token": token}).json()
        replay = self.call("job-claim", {"claim_token": token}).json()
        self.assertEqual(claim["job"]["analysis_id"], first["analysis_id"])
        self.assertEqual(replay, claim)
        self.assertEqual(self.call("job-get", {"id": second["analysis_id"]}, self.session).json()["status"], "queued")
        self.call("job-recover")

    def test_each_model_stage_requires_a_live_claim_and_deleted_context_cannot_continue(self):
        conversation = self.conversation()
        self.addCleanup(lambda: self.push([{"op_id": uid(), "entity_type": "conversations", "entity_uid": conversation,
            "base_version": 2, "action": "delete", "payload": {}}]))
        created = self.call("job-create", {"request_id": uid(), "conversation_uid": conversation,
            "context_revision": 1, "kind": "analysis"}, self.session).json()
        claim = self.call("job-claim", {"claim_token": "active-token-" + "x" * 51}).json()
        body = {"analysis_id": created["analysis_id"], "claim_token": claim["claim_token"]}
        self.assertEqual(self.call("job-check", body).status_code, 200)
        self.assertEqual(self.call("job-check", dict(body, claim_token="foreign-token")).status_code, 409)
        self.assertEqual(self.push([{"op_id": uid(), "entity_type": "conversations", "entity_uid": conversation,
            "base_version": 2, "action": "delete", "payload": {}}])[0]["status"], "applied")
        self.assertEqual(self.call("job-check", body).status_code, 409)
        self.assertEqual(self.call("job-get", {"id": created["analysis_id"]}, self.session).json()["status"], "cancelled")

    def test_cross_conversation_message_reassignment_does_not_change_context(self):
        conversation = self.conversation()
        page = self.call("pull", session=self.session).json()["changes"]
        original = next(c["record"] for c in page if c["record"]["entity_type"] == "messages")
        other = uid()
        self.push([{"op_id": uid(), "entity_type": "conversations", "entity_uid": other,
            "base_version": 0, "action": "upsert", "payload": {"profile_uid": original["payload"]["profile_uid"], "title": "Other"}}])
        changed = dict(original["payload"], conversation_uid=other)
        operation = {"op_id": uid(), "entity_type": "messages", "entity_uid": original["uid"],
                     "base_version": 1, "action": "upsert", "payload": changed}
        self.assertEqual(self.push([operation])[0]["status"], "rejected")

    def test_profile_delete_cleans_descendants_and_cancels_queued_job(self):
        conversation = self.conversation()
        created = self.call("job-create", {"request_id": uid(), "conversation_uid": conversation,
            "context_revision": 1, "kind": "analysis"}, self.session).json()
        changes = self.call("pull", session=self.session).json()["changes"]
        profile = next(c["record"] for c in changes if c["record"]["entity_type"] == "profiles")
        self.push([{"op_id": uid(), "entity_type": "profiles", "entity_uid": profile["uid"],
            "base_version": 1, "action": "delete", "payload": {}}])
        history = self.call("pull", session=self.session).json()
        self.assertNotIn("Confirmed test only", json.dumps(history))
        self.assertIsNone(self.call("job-claim").json()["job"])
        self.assertEqual(self.call("job-get", {"id": created["analysis_id"]}, self.session).json()["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
