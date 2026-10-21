"""Real disposable PocketBase: current intent belongs to one immutable job input."""
from contextlib import closing
import json
import sqlite3
import unittest

from services.pocketbase.tests import test_sync as fixture


INTENT = "我现在不困，还想自然地继续聊，不要替我结束对话。"


class CurrentIntentSnapshotTests(unittest.TestCase):
    setUpClass = classmethod(fixture.PocketBaseSyncTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.PocketBaseSyncTests.tearDownClass.__func__)
    setUp = fixture.PocketBaseSyncTests.setUp
    call = fixture.PocketBaseSyncTests.call
    push = fixture.PocketBaseSyncTests.push
    account = fixture.PocketBaseSyncTests.account
    login = fixture.PocketBaseSyncTests.login
    profile = fixture.PocketBaseSyncTests.profile
    conversation = fixture.PocketBaseSyncTests.conversation

    def records(self):
        latest = {}
        for change in self.call("pull", session=self.session).json()["changes"]:
            record = change["record"]
            latest[(record["entity_type"], record["uid"])] = record
        return latest

    def context(self):
        conversation = self.conversation()
        message = next(record for record in self.records().values()
                       if record["entity_type"] == "messages"
                       and record["payload"]["conversation_uid"] == conversation)
        self.addCleanup(lambda: self.push([dict(op_id=fixture.uid(), entity_type="profiles",
            entity_uid=message["payload"]["profile_uid"], base_version=1, action="delete", payload={})]))
        return conversation, message

    def request(self, conversation, **patch):
        return dict(request_id=fixture.uid(), conversation_uid=conversation,
                    context_revision=1, kind="analysis", **patch)

    def create(self, body):
        response = self.call("job-create", body, self.session)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def claim(self, accepted):
        response = self.call("job-claim")
        self.assertEqual(response.status_code, 200, response.text)
        claim = response.json()
        self.assertEqual(claim["job"]["analysis_id"], accepted["analysis_id"])
        return claim

    def finish(self, claim, **patch):
        job = claim["job"]
        result = dict(schema_version=1, analysis_id=job["analysis_id"],
            conversation_uid=job["conversation_uid"], context_revision=1,
            strategy="自然接话", candidates=[{"candidate_id": fixture.uid(), "text": "继续说说？"}])
        result.update(patch)
        response = self.call("job-finish", dict(analysis_id=job["analysis_id"],
            claim_token=claim["claim_token"], status="succeeded", result=result))
        self.assertEqual(response.status_code, 200, response.text)

    def persisted_input(self, accepted):
        with closing(sqlite3.connect(self.folder / "data/data.db")) as connection:
            row = connection.execute("SELECT input FROM cloud_jobs WHERE uid=?",
                                     (accepted["analysis_id"],)).fetchone()
        return json.loads(row[0])

    def test_intent_is_trimmed_in_immutable_job_input_and_never_saved_as_long_term_data(self):
        conversation, message = self.context()
        before = self.records()
        accepted = self.create(self.request(conversation, current_intent=" \n" + INTENT + "\t "))
        original_input = self.persisted_input(accepted)
        self.assertEqual(original_input["current_intent"], INTENT)
        claim = self.claim(accepted)
        self.assertEqual(claim["job"]["input"], original_input)
        self.assertEqual([m["text"] for m in original_input["messages"]], ["Confirmed test only"])
        self.assertEqual(original_input["messages"][0]["uid"], message["uid"])
        self.finish(claim)
        self.assertEqual(self.persisted_input(accepted), original_input)
        durable = {key: value for key, value in self.records().items() if key[0] != "analyses"}
        self.assertEqual(durable, before)
        self.assertNotIn(INTENT, json.dumps(list(durable.values()), ensure_ascii=False))

    def test_changed_intent_cannot_reuse_request_id_but_trimmed_replay_can(self):
        conversation, _ = self.context()
        body = self.request(conversation, current_intent=INTENT)
        first = self.create(body)
        self.assertEqual(self.create(dict(body, current_intent=" \n" + INTENT + " ")), first)
        changed = self.call("job-create", dict(body, current_intent="我现在想结束聊天。"), self.session)
        self.assertEqual(changed.status_code, 409, changed.text)
        self.assertEqual(changed.json()["error"]["code"], "request_id_reused")
        self.finish(self.claim(first))

    def test_blank_intent_and_omission_share_the_legacy_idempotency_digest(self):
        conversation, _ = self.context()
        body = self.request(conversation)
        first = self.create(body)
        self.assertEqual(self.create(dict(body, current_intent=" \n\t ")), first)
        self.assertEqual(self.create(dict(body, current_intent="")), first)
        claim = self.claim(first)
        self.assertEqual(claim["job"]["input"].get("current_intent", ""), "")
        self.finish(claim)

    def test_same_raw_chat_with_new_request_id_keeps_each_rounds_intent(self):
        conversation, _ = self.context()
        first = self.create(self.request(conversation, current_intent=INTENT))
        first_claim = self.claim(first)
        self.finish(first_claim)
        second_intent = "今天有点累，想礼貌结束。"
        second = self.create(self.request(conversation, current_intent=second_intent))
        second_claim = self.claim(second)
        self.assertNotEqual(second["analysis_id"], first["analysis_id"])
        self.assertEqual(first_claim["job"]["input"]["messages"], second_claim["job"]["input"]["messages"])
        self.assertEqual(second_claim["job"]["input"]["current_intent"], second_intent)
        self.assertEqual(self.persisted_input(first)["current_intent"], INTENT)
        self.finish(second_claim)

    def test_followups_inherit_the_selected_parent_input_instead_of_latest_round_or_model_result(self):
        conversation, _ = self.context()
        parent = self.create(self.request(conversation, current_intent=INTENT))
        self.finish(self.claim(parent), current_intent="模型虚构：用户想睡觉结束聊天。")
        latest = self.create(self.request(conversation, current_intent="我现在想结束对话。"))
        self.finish(self.claim(latest))
        for kind in ("rewrite", "explanation"):
            with self.subTest(kind=kind):
                body = self.request(conversation)
                body.update(kind=kind, parent_analysis_id=parent["analysis_id"])
                if kind == "rewrite":
                    body["selected_text"] = "继续说说？"
                followup = self.create(body)
                claim = self.claim(followup)
                snapshot = claim["job"]["input"]
                self.assertEqual(snapshot["current_intent"], INTENT)
                self.assertEqual(snapshot["parent_analysis_id"], parent["analysis_id"])
                self.assertEqual(snapshot["parent_result"]["current_intent"], "模型虚构：用户想睡觉结束聊天。")
                self.finish(claim)

    def test_legacy_parent_without_intent_cannot_gain_it_from_model_result(self):
        conversation, _ = self.context()
        parent = self.create(self.request(conversation))
        self.finish(self.claim(parent), current_intent="模型虚构补充")
        for kind in ("rewrite", "explanation"):
            with self.subTest(kind=kind):
                body = self.request(conversation)
                body.update(kind=kind, parent_analysis_id=parent["analysis_id"])
                if kind == "rewrite":
                    body["selected_text"] = "继续说说？"
                accepted = self.create(body)
                claim = self.claim(accepted)
                self.assertEqual(claim["job"]["input"].get("current_intent", ""), "")
                self.finish(claim)

    def test_invalid_intent_does_not_create_jobs_or_write_long_term_data(self):
        conversation, _ = self.context()
        before = self.records()
        for value in (1, True, [], {}, "补" * 2001):
            with self.subTest(value=type(value).__name__):
                response = self.call("job-create", self.request(conversation, current_intent=value), self.session)
                self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.records(), before)

    def test_followup_cannot_replace_parent_intent_but_blank_preserves_inheritance(self):
        conversation, _ = self.context()
        parent = self.create(self.request(conversation, current_intent=INTENT))
        self.finish(self.claim(parent))
        for kind in ("rewrite", "explanation"):
            for intent in (INTENT, " \t "):
                with self.subTest(kind=kind, intent=intent):
                    body = self.request(conversation, current_intent=intent)
                    body.update(kind=kind, parent_analysis_id=parent["analysis_id"])
                    if kind == "rewrite":
                        body["selected_text"] = "继续说说？"
                    response = self.call("job-create", body, self.session)
                    if intent.strip():
                        self.assertEqual(response.status_code, 400, response.text)
                        self.assertEqual(response.json()["error"]["code"], "invalid_current_intent")
                    else:
                        self.assertEqual(response.status_code, 202, response.text)
                        claim = self.claim(response.json())
                        self.assertEqual(claim["job"]["input"]["current_intent"], INTENT)
                        self.finish(claim)
