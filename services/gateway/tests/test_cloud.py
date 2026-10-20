import asyncio
import sys
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.main import Settings, Route, create_app


def test_login_is_public_but_account_endpoints_require_both_device_credentials():
    internal = "internal-" + "x" * 48
    requests = []
    def provider(request):
        requests.append(request)
        if request.url.path == "/internal/cloud/login":
            assert request.headers["X-Internal-Key"] == internal
            return httpx.Response(200, json={"token": "user-token", "user": {"id": "user1"},
                "device": {"id": "device1"}, "device_secret": "secret", "server_epoch": "epoch"})
        if request.url.path == "/internal/cloud/auth":
            if request.headers.get("X-Device-Secret") != "secret":
                return httpx.Response(401, json={"error": {"code": "unauthorized"}})
            return httpx.Response(200, json={"owner": "user1", "server_epoch": "epoch"})
        if request.url.path == "/internal/cloud/pull":
            return httpx.Response(200, json={"server_epoch": "epoch", "changes": [], "cursor": "cursor", "has_more": False})
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"code": 200})
        raise AssertionError("unexpected upstream call")
    settings = Settings(operator_token="operator-"+"x"*48, pb_url="http://127.0.0.1:8090",
        routes={"draft": Route("https://provider.example/v1", "model", "key")}, pb_internal_key=internal)
    with TestClient(create_app(settings, transport=httpx.MockTransport(provider), start_worker=False)) as api:
        login = api.post("/v1/auth/login", json={"email": "x@example.test", "password": "private-password",
            "device_name": "Test", "platform": "windows"})
        assert login.status_code == 200
        headers = {"Authorization": "Bearer user-token", "X-Device-Session": "device1", "X-Device-Secret": "secret"}
        config = api.get("/v1/config", headers=headers)
        assert config.status_code == 200 and config.json()["sync_ready"] is True
        assert api.get("/v1/sync/pull", headers=headers).status_code == 200
        assert api.post("/v1/chat/completions", json={"model": "draft", "messages": [{"role": "user", "content": "bypass"}]}, headers=headers).status_code == 403
        assert api.get("/v1/sync/pull", headers={"Authorization": "Bearer user-token"}).status_code == 401
        assert api.get("/v1/sync/pull", headers={"Authorization": "Bearer "+settings.operator_token}).status_code == 403
        assert "private-password" not in config.text


def test_cloud_pipeline_has_no_retry_and_preserves_evidence_and_null_weights():
    from app.pipeline import run_pipeline
    calls = []
    async def completion(alias, messages, max_tokens):
        calls.append(alias)
        if len(calls) == 1:
            return {"facts": ["Visible statement"], "hypotheses": ["Possible interpretation"],
                    "unknowns": ["Intent"], "strategy": "clarify", "stop_condition": "Stop if asked"}, {"total_tokens": 1}
        if len(calls) == 2:
            return {"candidates": ["First", "Second"]}, {"total_tokens": 2}
        return {"order": [1, 0]}, {"total_tokens": 1}
    job = {"analysis_id": "id", "conversation_uid": "conversation", "context_revision": 2,
        "kind": "analysis", "input": {"profile": {"name": "Person"}, "messages": [{"role": "other", "text": "Hello"}], "preferences": {}}}
    result = asyncio.run(run_pipeline(job, completion))
    assert calls == ["strategy", "draft", "strategy"]
    assert result["facts"] == ["Visible statement"]
    assert [c["text"] for c in result["candidates"]] == ["Second", "First"]
    assert all(c["weight"] is None for c in result["candidates"])
    assert result["ranking_status"] == "unavailable"
    assert result["usage"]["total_tokens"] == 4


def test_pipeline_transport_ambiguity_is_not_retried():
    from app.pipeline import run_pipeline
    count = 0
    async def completion(*args):
        nonlocal count
        count += 1
        raise httpx.ReadTimeout("secret upstream detail")
    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(run_pipeline({"analysis_id": "a", "conversation_uid": "c", "context_revision": 1,
            "kind": "analysis", "input": {"messages": []}}, completion))
    assert count == 1
