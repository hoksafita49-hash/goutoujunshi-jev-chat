import json
import asyncio
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.main import Route, Settings, create_app

TOKEN = "test-operator-token-" + "x" * 40
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
PROMPT = {"model": "draft", "messages": [{"role": "user", "content": "hello"}]}


def settings(**overrides):
    values = dict(
        operator_token=TOKEN,
        pb_url="http://127.0.0.1:8090",
        routes={"draft": Route("https://provider.example/v1", "actual-model", "provider-secret")},
        requests_per_minute=10,
    )
    values.update(overrides)
    return Settings(**values)


def client(handler, **overrides):
    return TestClient(create_app(settings(**overrides), transport=httpx.MockTransport(handler)))


def test_unauthorized_requests_never_reach_provider():
    def forbidden(_):
        pytest.fail("unauthorized request reached upstream")
    with client(forbidden) as api:
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            assert api.post("/v1/chat/completions", json=PROMPT, headers=headers).status_code == 401
        assert api.get("/v1/models").status_code == 401


def test_alias_selects_server_model_and_server_key():
    requests = []
    def upstream(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "reply-1", "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
        ], "model": "actual-model", "usage": {"total_tokens": 4}})
    with client(upstream) as api:
        result = api.post("/v1/chat/completions", json=PROMPT, headers=HEADERS)
    assert result.status_code == 200
    assert result.json()["choices"][0]["message"]["content"] == "hi"
    assert result.json()["model"] == "draft"
    assert len(requests) == 1
    assert str(requests[0].url) == "https://provider.example/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer provider-secret"
    assert json.loads(requests[0].content)["model"] == "actual-model"
    assert json.loads(requests[0].content)["stream"] is False


def test_missing_key_fails_closed_and_models_do_not_expose_config():
    def forbidden(_):
        pytest.fail("missing key must not make upstream request")
    with client(forbidden, routes={"draft": Route("https://provider.example/v1", "actual-model", "")}) as api:
        assert api.post("/v1/chat/completions", json=PROMPT, headers=HEADERS).status_code == 503
        response = api.get("/v1/models", headers=HEADERS)
        assert response.json()["data"] == []
        assert "provider" not in response.text


@pytest.mark.parametrize("change", [
    {"model": "unapproved"},
    {"stream": True},
    {"base_url": "http://127.0.0.1:8090"},
    {"api_key": "client-secret"},
    {"messages": [{"role": "tool", "content": "secret"}]},
    {"max_tokens": 1000000},
])
def test_rejects_unapproved_routes_and_unsupported_inputs(change):
    def forbidden(_):
        pytest.fail("invalid request reached upstream")
    with client(forbidden) as api:
        response = api.post("/v1/chat/completions", json=PROMPT | change, headers=HEADERS)
        assert response.status_code in (400, 422)
        assert "client-secret" not in response.text


def test_upstream_errors_are_sanitized():
    with client(lambda _: httpx.Response(401, text="provider-secret private prompt")) as api:
        response = api.post("/v1/chat/completions", json=PROMPT, headers=HEADERS)
        assert response.status_code == 502
        assert "provider-secret" not in response.text
        assert "private prompt" not in response.text


def test_timeout_is_not_automatically_retried():
    attempts = []
    def timeout(request):
        attempts.append(request)
        raise httpx.ReadTimeout("sensitive upstream detail")
    with client(timeout) as api:
        response = api.post("/v1/chat/completions", json=PROMPT, headers=HEADERS)
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "upstream_outcome_unknown"
    assert len(attempts) == 1
    assert "sensitive" not in response.text


def test_body_size_limit():
    def forbidden(_):
        pytest.fail("oversize request reached upstream")
    with client(forbidden, max_body_bytes=1024) as api:
        response = api.post("/v1/chat/completions", json=PROMPT | {
            "messages": [{"role": "user", "content": "x" * 2000}]
        }, headers=HEADERS)
        assert response.status_code == 413


def test_ready_checks_pocketbase_without_requiring_provider_key():
    with client(lambda _: httpx.Response(200, json={"code": 200}),
                routes={"draft": Route("https://provider.example/v1", "actual-model", "")}) as api:
        assert api.get("/health/live").status_code == 200
        assert api.get("/health/ready").status_code == 200
        config = api.get("/v1/config", headers=HEADERS).json()
        assert config["model_ready"] is False
        assert config["sync_ready"] is False
    with client(lambda _: httpx.Response(503)) as api:
        assert api.get("/health/ready").status_code == 503


def test_rate_limit_prevents_extra_paid_call():
    attempts = []
    def upstream(request):
        attempts.append(request)
        return httpx.Response(200, json={"choices": []})
    with client(upstream, requests_per_minute=1) as api:
        assert api.post("/v1/chat/completions", json=PROMPT, headers=HEADERS).status_code == 200
        assert api.post("/v1/chat/completions", json=PROMPT, headers=HEADERS).status_code == 429
    assert len(attempts) == 1


def test_weak_operator_token_and_plaintext_upstream_are_rejected():
    with pytest.raises(ValueError):
        settings(operator_token="short")
    with pytest.raises(ValueError):
        Route("http://provider.example/v1", "model", "key")


def test_stalled_request_body_times_out_before_route_execution():
    async def exercise():
        app = create_app(settings(body_timeout_seconds=0.02))
        messages = []
        async def receive():
            await asyncio.sleep(30)
            return {"type": "http.request", "body": b"", "more_body": True}
        async def send(message):
            messages.append(message)
        await app({"type": "http", "method": "GET", "path": "/health/live",
                   "headers": [], "query_string": b""}, receive, send)
        return messages
    result = asyncio.run(exercise())
    assert result[0]["status"] == 408
    assert json.loads(result[1]["body"])["error"]["code"] == "request_body_timeout"
