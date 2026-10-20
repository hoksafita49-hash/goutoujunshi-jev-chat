"""Provider response contracts observed during synthetic live acceptance."""
import asyncio
from collections import deque
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.cloud import model_json
from app.main import Route
from app.pipeline import PipelineError, run_pipeline


def completion(output, *, finish_reason="stop"):
    return {"id": "synthetic-completion", "object": "chat.completion", "created": 1,
        "model": "fixture-model", "choices": [{"index": 0,
            "message": {"role": "assistant", "content": output},
            "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}


async def exercise(responses, operation):
    calls = []
    def provider(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=responses[len(calls) - 1])
    route = Route("https://provider.example/v1", "fixture-model", "fixture-key", disable_thinking=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as upstream:
        app = SimpleNamespace(state=SimpleNamespace(
            settings=SimpleNamespace(routes={"strategy": route, "draft": route}),
            starts=deque(), inflight=asyncio.Semaphore(2), upstream=upstream))
        return await operation(app, calls)


@pytest.mark.parametrize("nested", [False, True])
def test_analysis_canonicalizes_observed_nested_action_and_stop_condition(nested):
    strategy = {"action": "Confirm the time", "stop_condition": "Stop if declined"} if nested else "Confirm the time"
    evidence = {"evidence": [{"kind": "fact", "text": "Saturday is available",
        "source_quotes": [{"message_uid": "message-one", "quote": "Saturday works"}]}],
        "strategy": strategy}
    if not nested:
        evidence["stop_condition"] = "Stop if declined"
    responses = [completion(json.dumps(evidence)), completion('{"candidates":["Would 2 pm work?"]}'),
        completion('{"order":[0]}')]
    job = {"analysis_id": "analysis-one", "conversation_uid": "conversation-one", "context_revision": 1,
        "kind": "analysis", "input": {"messages": [{"uid": "message-one", "version": 1,
            "role": "other", "text": "Saturday works"}], "preferences": {"count": 1}}}

    async def run(app, calls):
        result = await run_pipeline(job, lambda *args: model_json(app, *args))
        assert result["strategy"] == "Confirm the time"
        assert result["stop_condition"] == "Stop if declined"
        assert result["evidence"][0]["citation_status"] == "verified"
        assert result["candidates"][0]["text"] == "Would 2 pm work?"
        assert result["usage"]["total_tokens"] == 15
        assert [call["max_tokens"] for call in calls] == [1024, 768, 128]
        assert all(call["thinking"] == {"type": "disabled"} for call in calls)
        assert all(call["response_format"] == {"type": "json_object"} for call in calls)
        draft_evidence = json.loads(calls[1]["messages"][1]["content"])["evidence"]
        assert draft_evidence["strategy"] == "Confirm the time"
        assert draft_evidence["stop_condition"] == "Stop if declined"
    asyncio.run(exercise(responses, run))


@pytest.mark.parametrize("strategy", [
    {"action": "Confirm the time"},
    {"action": ["Confirm the time"], "stop_condition": "Stop if declined"},
    {"action": "Confirm the time", "stop_condition": {"when": "declined"}},
    {"action": "Confirm the time", "stop_condition": "Stop if declined", "other": "unknown"},
])
def test_unrecognized_strategy_objects_fail_without_another_model_request(strategy):
    responses = [completion(json.dumps({"evidence": [], "strategy": strategy}))]
    job = {"analysis_id": "a", "conversation_uid": "c", "context_revision": 1,
        "kind": "analysis", "input": {"messages": []}}
    async def run(app, calls):
        with pytest.raises(PipelineError, match="^invalid_model_output$"):
            await run_pipeline(job, lambda *args: model_json(app, *args))
        assert len(calls) == 1
    asyncio.run(exercise(responses, run))


def test_nested_strategy_cannot_overwrite_a_conflicting_top_level_stop_condition():
    responses = [completion(json.dumps({"evidence": [],
        "strategy": {"action": "Confirm the time", "stop_condition": "Nested boundary"},
        "stop_condition": "Top-level boundary"})),
        completion('{"candidates":["One reply"]}'), completion('{"order":[0]}')]
    job = {"analysis_id": "a", "conversation_uid": "c", "context_revision": 1,
        "kind": "analysis", "input": {"messages": []}}
    async def run(app, calls):
        with pytest.raises(PipelineError, match="^invalid_model_output$"):
            await run_pipeline(job, lambda *args: model_json(app, *args))
        assert len(calls) == 1
    asyncio.run(exercise(responses, run))


@pytest.mark.parametrize("output,finish_reason,error", [
    ("", "stop", "invalid_model_output"),
    ("[]", "stop", "invalid_model_output"),
    ('{"strategy":', "stop", "invalid_model_output"),
    ('{"strategy":"complete-looking"}', "length", "incomplete_model_output"),
])
def test_invalid_or_incomplete_json_still_fails_without_retry(output, finish_reason, error):
    async def run(app, calls):
        with pytest.raises(PipelineError, match="^" + error + "$"):
            await model_json(app, "strategy", [{"role": "user", "content": "Synthetic JSON only"}], 128)
        assert len(calls) == 1
    asyncio.run(exercise([completion(output, finish_reason=finish_reason)], run))
