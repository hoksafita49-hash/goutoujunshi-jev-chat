"""Per-round user intent is explicit input, never invented chat evidence."""
import asyncio
import json
from pathlib import Path
import sys
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.main import Route, Settings, create_app
from app.pipeline import run_pipeline


INTENT = "我现在不困，还想自然地继续聊，不要替我结束对话。"
HEADERS = {"Authorization": "Bearer test-account", "X-Device-Session": "test-device",
           "X-Device-Secret": "test-secret"}


def analysis_request(**patch):
    return dict(request_id=str(uuid.uuid4()), conversation_uid=str(uuid.uuid4()),
                context_revision=1, kind="analysis", **patch)


@pytest.fixture
def intent_api():
    forwarded = []

    def pocketbase(request):
        assert request.url.host == "127.0.0.1", "Test attempted a real model request"
        if request.url.path.endswith("/auth"):
            return httpx.Response(200, json={"owner": "test-account", "server_epoch": "test-epoch"})
        if request.url.path.endswith("/job-create"):
            forwarded.append(json.loads(request.content))
            return httpx.Response(200, json={"analysis_id": str(uuid.uuid4()), "status": "queued"})
        raise AssertionError(f"Unexpected isolated upstream route: {request.url.path}")

    settings = Settings(operator_token="operator-" + "x" * 48, pb_url="http://127.0.0.1:8090",
        routes={alias: Route("https://provider.example/v1", "fixture", "fixture-key")
                for alias in ("draft", "strategy")}, pb_internal_key="internal-" + "x" * 48)
    with TestClient(create_app(settings, transport=httpx.MockTransport(pocketbase), start_worker=False)) as api:
        yield api, forwarded


def test_first_analysis_forwards_trimmed_intent_without_changing_chat_fields(intent_api):
    api, forwarded = intent_api
    body = analysis_request(current_intent=" \n" + INTENT + "\t ")
    response = api.post("/v1/analyses", headers=HEADERS, json=body)
    assert response.status_code == 202, response.text
    assert forwarded[0]["current_intent"] == INTENT
    assert forwarded[0]["conversation_uid"] == body["conversation_uid"]
    assert "messages" not in forwarded[0]


def test_blank_intent_uses_the_same_wire_shape_as_omission(intent_api):
    api, forwarded = intent_api
    body = analysis_request()
    assert api.post("/v1/analyses", headers=HEADERS, json=body).status_code == 202
    response = api.post("/v1/analyses", headers=HEADERS, json=dict(body, current_intent=" \n\t "))
    assert response.status_code == 202, response.text
    assert forwarded[1] == forwarded[0]
    assert "current_intent" not in forwarded[0]


@pytest.mark.parametrize("value", [1, 1.5, True, [], {}, "补" * 2001])
def test_invalid_current_intent_is_rejected_before_job_creation(intent_api, value):
    api, forwarded = intent_api
    response = api.post("/v1/analyses", headers=HEADERS,
                        json=analysis_request(current_intent=value))
    assert response.status_code == 422, response.text
    assert forwarded == []


def test_current_intent_accepts_the_full_2000_character_limit(intent_api):
    api, forwarded = intent_api
    response = api.post("/v1/analyses", headers=HEADERS,
                        json=analysis_request(current_intent="补" * 2000))
    assert response.status_code == 202, response.text
    assert forwarded[0]["current_intent"] == "补" * 2000


@pytest.mark.parametrize("kind", ["rewrite", "explanation"])
def test_followup_rejects_a_nonempty_client_intent_override(intent_api, kind):
    api, forwarded = intent_api
    body = analysis_request()
    body.update(kind=kind, parent_analysis_id=str(uuid.uuid4()), current_intent=INTENT)
    if kind == "rewrite":
        body["selected_text"] = "继续说说？"
    response = api.post("/v1/analyses", headers=HEADERS, json=body)
    assert response.status_code == 422, response.text
    assert forwarded == []


@pytest.mark.parametrize("kind", ["rewrite", "explanation"])
def test_blank_followup_intent_is_omitted_so_pocketbase_can_inherit_parent(intent_api, kind):
    api, forwarded = intent_api
    body = analysis_request()
    body.update(kind=kind, parent_analysis_id=str(uuid.uuid4()), current_intent=" \t ")
    if kind == "rewrite":
        body["selected_text"] = "继续说说？"
    response = api.post("/v1/analyses", headers=HEADERS, json=body)
    assert response.status_code == 202, response.text
    assert "current_intent" not in forwarded[0]


def assert_current_intent_prompt(messages):
    system = messages[0]["content"]
    for concept in ("current_intent", "优先", "档案", "推测", "睡意", "结束", "原文", "证据"):
        assert concept in system, f"Missing current-intent boundary {concept!r}: {system}"
    body = json.loads(messages[1]["content"])
    context = body.get("context", body)
    assert context["current_intent"] == INTENT
    assert context["messages"] == [{"uid": "message-one", "version": 1,
                                    "role": "other", "text": "今天怎么样？"}]


def test_evidence_draft_and_ranking_use_current_intent_without_forged_chat_citations():
    calls = []

    async def complete(alias, messages, tokens):
        calls.append(messages)
        if len(calls) == 1:
            return {"evidence": [{"kind": "fact", "text": "用户想继续聊",
                "source_message_uids": ["message-one"],
                "source_quotes": [{"message_uid": "message-one", "quote": INTENT}]}],
                "strategy": "自然接话", "stop_condition": "对方明确拒绝时停止推进"}, {}
        if len(calls) == 2:
            return {"candidates": ["今天还不错，你呢？"]}, {}
        return {"order": [0]}, {}

    context = {"current_intent": INTENT,
        "profile": {"notes": "旧档案：用户通常很早睡"},
        "messages": [{"uid": "message-one", "version": 1, "role": "other", "text": "今天怎么样？"}],
        "preferences": {"count": 1}}
    job = dict(analysis_id="analysis", conversation_uid="conversation", context_revision=1,
               kind="analysis", input=context)
    result = asyncio.run(run_pipeline(job, complete))
    assert len(calls) == 3
    for messages in calls:
        assert_current_intent_prompt(messages)
    assert result["evidence"][0]["citation_status"] == "unavailable"
    assert result["evidence"][0]["source_quotes"] == []
    assert result["evidence"][0]["source_message_uids"] == []
    assert context["current_intent"] == INTENT


@pytest.mark.parametrize("kind", ["rewrite", "explanation"])
def test_followup_prompt_preserves_parent_user_intent_over_model_inference(kind):
    calls = []

    async def complete(alias, messages, tokens):
        calls.append(messages)
        return {"text": "今天还不错，你呢？"}, {}

    job = dict(analysis_id="followup", conversation_uid="conversation", context_revision=1,
        kind=kind, input={"current_intent": INTENT,
            "messages": [{"uid": "message-one", "version": 1, "role": "other", "text": "今天怎么样？"}],
            "parent_result": {"current_intent": "模型推测：用户困了想结束", "strategy": "继续聊"},
            "selected_text": "今天还不错，你呢？"})
    result = asyncio.run(run_pipeline(job, complete))
    assert len(calls) == 1
    assert_current_intent_prompt(calls[0])
    assert result["text"] == "今天还不错，你呢？"
