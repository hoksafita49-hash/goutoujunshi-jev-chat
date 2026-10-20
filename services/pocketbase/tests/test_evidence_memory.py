"""Real isolated PB: provenance, revision history and privacy deletion."""
import copy
import json
import sqlite3
from contextlib import closing
import unittest
from services.pocketbase.tests import test_sync as fixture


class EvidenceMemoryTests(unittest.TestCase):
    setUpClass = classmethod(fixture.PocketBaseSyncTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.PocketBaseSyncTests.tearDownClass.__func__)
    setUp = fixture.PocketBaseSyncTests.setUp
    call = fixture.PocketBaseSyncTests.call
    push = fixture.PocketBaseSyncTests.push
    account = fixture.PocketBaseSyncTests.account
    login = fixture.PocketBaseSyncTests.login
    profile = fixture.PocketBaseSyncTests.profile
    conversation = fixture.PocketBaseSyncTests.conversation

    def operation(self, kind, payload, entity_uid=None, version=0, action="upsert"):
        return dict(op_id=fixture.uid(), entity_type=kind, entity_uid=entity_uid or fixture.uid(),
                    base_version=version, action=action, payload=payload)

    def records(self):
        page = self.call("pull", session=self.session).json()
        latest = {}
        for item in page["changes"]:
            row = item["record"]
            latest[(row["entity_type"], row["uid"])] = row
        return list(latest.values())

    def context(self):
        conversation = self.conversation()
        message = next(r for r in self.records() if r["entity_type"] == "messages"
                       and r["payload"].get("conversation_uid") == conversation)
        return conversation, message["payload"]["profile_uid"], message

    def memory(self, profile, sources=(), text="Verified detail", status="pending"):
        payload = dict(profile_uid=profile, text=text, kind="fact", status=status,
                       source_uids=list(sources), change_reason="创建记忆")
        op = self.operation("memories", payload)
        result = self.push([op])[0]
        self.assertEqual(result["status"], "applied", result)
        return op, result["record"]

    def job(self, conversation, preferences=None):
        current = next(r for r in self.records() if r["uid"] == conversation)
        revision = current["payload"]["context_revision"]
        data = dict(request_id=fixture.uid(), conversation_uid=conversation, context_revision=revision, kind="analysis")
        if preferences is not None:
            data["preferences"] = preferences
        response = self.call("job-create", data, self.session)
        self.assertEqual(response.status_code, 202, response.text)
        target = response.json()["analysis_id"]
        for _ in range(20):
            claim = self.call("job-claim").json()
            job = claim["job"]
            self.assertIsNotNone(job)
            result = dict(schema_version=1, analysis_id=job["analysis_id"],
                          conversation_uid=job["conversation_uid"], context_revision=1,
                          facts=["Confirmed test only"], hypotheses=[], unknowns=[], candidates=[],
                          evidence=[dict(claim_id="fact-1", kind="fact", text="Confirmed test only",
                                         source_message_uids=[job["input"]["messages"][0]["uid"]],
                                         source_quotes=[dict(message_uid=job["input"]["messages"][0]["uid"], quote="Confirmed test only")],
                                         citation_status="verified")])
            finished = self.call("job-finish", dict(analysis_id=job["analysis_id"],
                claim_token=claim["claim_token"], status="succeeded", result=result))
            self.assertEqual(finished.status_code, 200)
            if job["analysis_id"] == target:
                return job
        self.fail("Unable to claim own isolated fixture job")

    def test_analysis_override_merges_saved_preferences_and_explicit_style_clear(self):
        conversation, _, _ = self.context()
        prefs = dict(tone="自然", length="简短", count=2, style="少用表情", future_option="keep")
        self.push([self.operation("preferences", prefs, "00000000-0000-4000-8000-000000000001")])
        job = self.job(conversation, {"style": ""})
        self.assertEqual(job["input"]["preferences"], dict(prefs, style=""))

    def test_invalid_count_is_rejected_without_creating_a_job(self):
        conversation, _, _ = self.context()
        for value in (0, 4, True, "2", None):
            with self.subTest(value=value):
                response = self.call("job-create", dict(request_id=fixture.uid(), conversation_uid=conversation,
                    context_revision=1, kind="analysis", preferences={"count": value}), self.session)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], "invalid_preferences")

    def test_memory_sources_must_be_same_person_and_account(self):
        _, profile, message = self.context()
        _, another_profile, _ = self.context()
        payload = dict(profile_uid=another_profile, text="Wrong person", kind="fact", status="pending",
                       source_uids=[message["uid"]])
        self.assertEqual(self.push([self.operation("memories", payload)])[0]["code"], "memory_source_mismatch")
        other = self.login(self.account())
        foreign_profile = self.profile("Other account")
        self.push([foreign_profile], other)
        payload["profile_uid"] = foreign_profile["entity_uid"]
        self.assertEqual(self.push([self.operation("memories", payload)], other)[0]["code"], "memory_source_unavailable")
        _, saved = self.memory(profile, [message["uid"]])
        self.assertEqual(saved["payload"]["source_refs"], [{"message_uid": message["uid"], "version": 1}])

    def test_empty_text_hypothesis_confirmation_and_forged_sources_are_rejected(self):
        _, profile, _ = self.context()
        base = dict(profile_uid=profile, text="detail", kind="fact", status="pending", source_uids=[])
        for patch, code in (({"text": "  "}, "invalid_memory"),
                            ({"kind": "hypothesis", "status": "confirmed"}, "invalid_memory_status"),
                            ({"source_uids": [fixture.uid()]}, "memory_source_unavailable"),
                            ({"source_uids": "not a list"}, "invalid_memory")):
            with self.subTest(code=code):
                result = self.push([self.operation("memories", dict(base, **patch))])[0]
                self.assertEqual(result["status"], "rejected")
                self.assertEqual(result["code"], code)

    def test_confirmation_correction_history_pages_replay_and_restore_as_new_pending(self):
        _, profile, message = self.context()
        op, saved = self.memory(profile, [message["uid"]])
        confirmed = self.operation("memories", dict(saved["payload"], status="confirmed", change_reason="用户确认"), saved["uid"], 1)
        self.assertEqual(self.push([confirmed])[0]["record"]["version"], 2)
        corrected = self.operation("memories", dict(saved["payload"], text="Corrected detail", change_reason="原记录有误"), saved["uid"], 2)
        first = self.push([corrected])[0]
        self.assertEqual(self.push([corrected])[0], first)
        history = self.call("memory-history", {"id": saved["uid"], "limit": 2}, self.session)
        self.assertEqual(history.status_code, 200, history.text)
        page = history.json()
        self.assertEqual([r["version"] for r in page["versions"]], [3, 2])
        self.assertTrue(page["has_more"])
        older = self.call("memory-history", {"id": saved["uid"], "limit": 2,
            "before_version": page["next_before_version"]}, self.session).json()
        self.assertEqual([r["version"] for r in older["versions"]], [1])
        restored = dict(older["versions"][0]["payload"], status="pending", change_reason="恢复版本 1")
        restore = self.operation("memories", restored, saved["uid"], 3)
        self.assertEqual(self.push([restore])[0]["record"]["version"], 4)
        stale = self.push([self.operation("memories", restored, saved["uid"], 3)])[0]
        self.assertEqual(stale["status"], "conflict")
        foreign = self.login(self.account())
        self.assertEqual(self.call("memory-history", {"id": saved["uid"]}, foreign).status_code, 404)

    def test_source_correction_returns_memory_pending_and_evidence_keeps_analysis_snapshot(self):
        conversation, profile, message = self.context()
        job = self.job(conversation)
        _, memory = self.memory(profile, [message["uid"]], status="confirmed")
        update = self.operation("messages", dict(message["payload"], text="Corrected source"), message["uid"], 1)
        self.assertEqual(self.push([update])[0]["status"], "applied")
        current = next(r for r in self.records() if r["uid"] == memory["uid"])
        self.assertEqual(current["payload"]["status"], "pending")
        self.assertEqual(current["version"], 2)
        evidence = self.call("job-evidence", {"id": job["analysis_id"]}, self.session)
        self.assertEqual(evidence.status_code, 200, evidence.text)
        self.assertEqual(evidence.json()["messages"][0]["text"], "Confirmed test only")
        self.assertEqual(evidence.json()["messages"][0]["version"], 1)

    def test_source_deletion_scrubs_derived_memory_analysis_history_and_replay(self):
        conversation, profile, message = self.context()
        job = self.job(conversation)
        op, memory = self.memory(profile, [message["uid"]], text="Private derived detail", status="confirmed")
        deletion = self.operation("messages", {}, message["uid"], 1, "delete")
        self.assertEqual(self.push([deletion])[0]["status"], "applied")
        self.assertEqual(self.call("memory-history", {"id": memory["uid"]}, self.session).status_code, 404)
        self.assertEqual(self.call("job-evidence", {"id": job["analysis_id"]}, self.session).status_code, 404)
        self.assertEqual(self.call("job-get", {"id": job["analysis_id"]}, self.session).json()["status"], "cancelled")
        replay = self.push([op])[0]
        self.assertEqual(replay["record"]["payload"], {})
        page = self.call("pull", session=self.session).text
        self.assertNotIn("Private derived detail", page)
        self.assertNotIn("Confirmed test only", page)
        self.assertEqual(self.push([self.operation("memories", op["payload"], memory["uid"], 2)])[0]["code"], "entity_deleted")

    def test_history_and_evidence_require_active_device(self):
        conversation, profile, _ = self.context()
        _, memory = self.memory(profile)
        job = self.job(conversation)
        other = self.login(self.account())
        self.assertEqual(self.call("job-evidence", {"id": job["analysis_id"]}, other).status_code, 404)
        self.call("logout", session=self.session)
        for action, id_ in (("job-evidence", job["analysis_id"]), ("memory-history", memory["uid"])):
            self.assertEqual(self.call(action, {"id": id_}, self.session).status_code, 401)

    def test_removing_source_from_current_memory_cannot_hide_deleted_historical_source(self):
        _, profile, message = self.context()
        _, memory = self.memory(profile, [message["uid"]])
        changed = dict(memory["payload"], text="Manual correction", source_uids=[], change_reason="用户改记")
        self.assertEqual(self.push([self.operation("memories", changed, memory["uid"], 1)])[0]["status"], "applied")
        self.assertEqual(self.push([self.operation("messages", {}, message["uid"], 1, "delete")])[0]["status"], "applied")
        self.assertEqual(self.call("memory-history", {"id": memory["uid"]}, self.session).status_code, 404)

    def test_changing_confirmed_text_requires_a_new_explicit_confirmation(self):
        _, profile, message = self.context()
        _, memory = self.memory(profile, [message["uid"]], status="confirmed")
        changed = dict(memory["payload"], text="Different statement", status="confirmed", change_reason="纠正")
        update = self.push([self.operation("memories", changed, memory["uid"], 1)])[0]
        self.assertEqual(update["record"]["payload"]["status"], "pending")
        confirmed = dict(update["record"]["payload"], status="confirmed", change_reason="重新确认")
        self.assertEqual(self.push([self.operation("memories", confirmed, memory["uid"], 2)])[0]["record"]["payload"]["status"], "confirmed")

    def test_source_deletion_scrubs_child_jobs_after_source_leaves_the_context_window(self):
        conversation, profile, message = self.context()
        parent = self.job(conversation)
        for group in (range(50), range(50, 64)):
            ops = [self.operation("messages", dict(profile_uid=profile, conversation_uid=conversation,
                    role="me", text="Later message " + str(index), confirmed=True,
                    observed_at="2026-10-21T01:00:00Z", order_key=f"1{index:05}")) for index in group]
            self.assertTrue(all(r["status"] == "applied" for r in self.push(ops)))
        accepted = self.call("job-create", dict(request_id=fixture.uid(), conversation_uid=conversation,
            context_revision=65, kind="explanation", parent_analysis_id=parent["analysis_id"]), self.session)
        self.assertEqual(accepted.status_code, 202, accepted.text)
        child = self.call("job-claim").json()
        self.assertFalse(any(m["uid"] == message["uid"] for m in child["job"]["input"]["messages"]))
        self.assertIn("Confirmed test only", json.dumps(child["job"]["input"]["parent_result"]))
        self.assertEqual(self.call("job-finish", dict(analysis_id=child["job"]["analysis_id"],
            claim_token=child["claim_token"], status="succeeded", result={"text": "Confirmed test only"})).status_code, 200)
        self.assertEqual(self.push([self.operation("messages", {}, message["uid"], 1, "delete")])[0]["status"], "applied")
        saved = self.call("job-get", {"id": child["job"]["analysis_id"]}, self.session).json()
        self.assertEqual(saved["status"], "cancelled")
        self.assertIsNone(saved["result"])
        self.assertEqual(self.call("job-evidence", {"id": child["job"]["analysis_id"]}, self.session).status_code, 404)
        self.assertNotIn("Confirmed test only", self.call("pull", session=self.session).text)

    def test_deleting_corrected_memory_scrubs_pre_upgrade_uidless_job_snapshot(self):
        conversation, profile, _ = self.context()
        _, memory = self.memory(profile, text="Private pre-upgrade detail", status="confirmed")
        job = self.job(conversation)
        # Exact v1 shape: snapshots had memory text/profile but no memory UID.
        with closing(sqlite3.connect(self.folder / "data/data.db")) as connection:
            row = connection.execute("SELECT input FROM cloud_jobs WHERE uid=?", (job["analysis_id"],)).fetchone()
            data = json.loads(row[0])
            data["memories"][0].pop("uid")
            connection.execute("UPDATE cloud_jobs SET input=? WHERE uid=?", (json.dumps(data), job["analysis_id"]))
            connection.commit()
        corrected = dict(memory["payload"], text="Corrected current detail", change_reason="纠正记录")
        self.assertEqual(self.push([self.operation("memories", corrected, memory["uid"], 1)])[0]["status"], "applied")
        self.assertEqual(self.push([self.operation("memories", {}, memory["uid"], 2, "delete")])[0]["status"], "applied")
        self.assertEqual(self.call("job-get", {"id": job["analysis_id"]}, self.session).json()["status"], "cancelled")
        with closing(sqlite3.connect(self.folder / "data/data.db")) as connection:
            row = connection.execute("SELECT input FROM cloud_jobs WHERE uid=?", (job["analysis_id"],)).fetchone()
            self.assertNotIn("Private pre-upgrade detail", row[0])

    def test_effective_confirmed_memory_changes_invalidate_cached_person_contexts(self):
        conversation, profile, _ = self.context()
        def revision():
            return next(r for r in self.records() if r["uid"] == conversation)["payload"]["context_revision"]
        _, memory = self.memory(profile)
        self.assertEqual(revision(), 1)  # pending does not affect analysis input
        confirmed = dict(memory["payload"], status="confirmed", change_reason="已核对")
        self.assertEqual(self.push([self.operation("memories", confirmed, memory["uid"], 1)])[0]["status"], "applied")
        self.assertEqual(revision(), 2)
        stale = self.call("job-create", dict(request_id=fixture.uid(), conversation_uid=conversation,
            context_revision=1, kind="analysis"), self.session)
        self.assertEqual(stale.status_code, 409)
        corrected = dict(confirmed, text="Correction", status="confirmed", change_reason="纠正")
        self.assertEqual(self.push([self.operation("memories", corrected, memory["uid"], 2)])[0]["status"], "applied")
        self.assertEqual(revision(), 3)  # confirmed -> pending removes the old fact
        self.assertEqual(self.push([self.operation("memories", dict(corrected, status="pending"), memory["uid"], 3)])[0]["status"], "applied")
        self.assertEqual(revision(), 3)

    def test_cascade_delete_keeps_monotonic_versions_and_initial_receipt_matches_replay(self):
        conversation, profile, message = self.context()
        for index in range(3):
            self.memory(profile, [message["uid"]], text="Detail " + str(index), status="confirmed")
        current = next(r for r in self.records() if r["uid"] == conversation)
        deletion = self.operation("conversations", {}, conversation, current["version"], "delete")
        first = self.push([deletion])[0]
        self.assertIsNotNone(first["record"]["deleted_at"])
        self.assertEqual(first["record"]["payload"], {})
        self.assertEqual(self.push([deletion])[0], first)
        changes = self.call("pull", session=self.session).json()["changes"]
        versions = [item["record"]["version"] for item in changes if item["record"]["uid"] == conversation]
        self.assertEqual(versions, list(range(1, max(versions) + 1)))
