"""Real PB + real gateway + fake paid provider: cross-device end-to-end contract."""
import importlib.util
import json
from pathlib import Path
import sys
import time
import tempfile
import threading
import socket
import types
import uuid

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services/gateway"))
from app.main import Settings, Route, create_app


@pytest.mark.parametrize("interruption", ["lost_claim_response", "delete_after_first_model"])
def test_worker_survives_claim_response_loss_and_stops_new_model_calls_after_deletion(interruption):
    spec = importlib.util.spec_from_file_location("pb_worker_fixture", ROOT / "services/pocketbase/tests/test_sync.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    PB = fixture.PocketBaseSyncTests
    PB.setUpClass()
    context = PB()
    context.setUp()
    conversation = context.conversation()
    model_calls = []
    dropped_claim = False
    finished = threading.Event()
    passthrough = httpx.AsyncHTTPTransport()

    async def routing(request):
        nonlocal dropped_claim
        if request.url.host == "127.0.0.1":
            response = await passthrough.handle_async_request(request)
            await response.aread()
            if (interruption == "lost_claim_response" and not dropped_claim
                    and request.url.path.endswith("/job-claim") and response.json().get("job")):
                dropped_claim = True
                raise httpx.ReadTimeout("Fixture dropped the already committed claim response", request=request)
            if request.url.path.endswith("/job-finish"):
                finished.set()
            return response
        model_calls.append(json.loads(request.content))
        output = [
            {"facts": ["Confirmed"], "strategy": "clarify"},
            {"candidates": ["One reply"]}, {"order": [0]},
        ][len(model_calls)-1]
        if interruption == "delete_after_first_model" and len(model_calls) == 1:
            deletion = context.push([{"op_id": str(uuid.uuid4()), "entity_type": "conversations",
                "entity_uid": conversation, "base_version": 2, "action": "delete", "payload": {}}])
            assert deletion[0]["status"] == "applied"
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(output)},
            "finish_reason": "stop"}], "usage": {"total_tokens": 1}})

    settings = Settings(operator_token="operator-"+"x"*48, pb_url=PB.url,
        routes={alias: Route("https://provider.example/v1", "fixture", "fixture-key") for alias in ("draft", "strategy")},
        pb_internal_key=fixture.INTERNAL)
    try:
        with TestClient(create_app(settings, transport=httpx.MockTransport(routing))) as api:
            headers = {"Authorization": "Bearer " + context.session["token"],
                "X-Device-Session": context.session["device"]["id"], "X-Device-Secret": context.session["device_secret"]}
            accepted = api.post("/v1/analyses", headers=headers, json={"request_id": str(uuid.uuid4()),
                "conversation_uid": conversation, "context_revision": 1, "kind": "analysis"})
            assert accepted.status_code == 202
            assert finished.wait(timeout=8), "Claimed job did not converge while the gateway stayed alive"
            result = api.get("/v1/analyses/" + accepted.json()["analysis_id"], headers=headers).json()
            if interruption == "lost_claim_response":
                assert dropped_claim
                assert result["status"] == "succeeded", result
                assert len(model_calls) == 3
            else:
                assert result["status"] == "cancelled", result
                assert len(model_calls) == 1, "A deleted conversation triggered more model generations"
    finally:
        PB.tearDownClass()


def test_three_devices_sync_and_share_one_durable_analysis_without_rebilling():
    spec = importlib.util.spec_from_file_location("pb_fixture", ROOT / "services/pocketbase/tests/test_sync.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    PB = fixture.PocketBaseSyncTests
    PB.setUpClass()
    provider_calls = []
    passthrough = httpx.AsyncHTTPTransport()
    async def routing(request):
        if request.url.host == "127.0.0.1":
            return await passthrough.handle_async_request(request)
        assert request.url.host == "provider.example"
        assert request.headers["Authorization"] == "Bearer fixture-key"
        provider_calls.append(json.loads(request.content))
        outputs = [
            {"facts": ["Confirmed fact"], "hypotheses": [], "unknowns": ["Intent"], "strategy": "clarify", "stop_condition": "Respect refusal"},
            {"candidates": ["First reply", "Second reply"]}, {"order": [1, 0]},
        ]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(outputs[len(provider_calls)-1])}, "finish_reason": "stop"}], "usage": {"total_tokens": 3}})
    settings = Settings(operator_token="operator-"+"x"*48, pb_url=PB.url,
        routes={alias: Route("https://provider.example/v1", "fixture-model", "fixture-key") for alias in ("draft", "strategy")},
        pb_internal_key=fixture.INTERNAL)
    try:
        response = PB.client.post("/internal/cloud/provision", headers={"X-Internal-Key": fixture.INTERNAL},
            json={"email": "devices@example.test", "password": fixture.PASSWORD})
        assert response.status_code == 200
        with TestClient(create_app(settings, transport=httpx.MockTransport(routing))) as api:
            sessions = []
            for platform in ("windows", "android", "ios"):
                login = api.post("/v1/auth/login", json={"email": "devices@example.test", "password": fixture.PASSWORD,
                    "device_name": platform, "platform": platform})
                assert login.status_code == 200
                session = login.json()
                sessions.append(({"Authorization": "Bearer "+session["token"],
                    "X-Device-Session": session["device"]["id"], "X-Device-Secret": session["device_secret"]}, session))
            headers, session = sessions[0]
            profile, conversation, message = (str(uuid.uuid4()).upper() for _ in range(3))
            def operation(kind, entity_uid, payload):
                return {"op_id": str(uuid.uuid4()).upper(), "entity_type": kind, "entity_uid": entity_uid,
                        "base_version": 0, "action": "upsert", "payload": payload}
            push = {"server_epoch": session["server_epoch"], "operations": [
                operation("profiles", profile, {"name": "Synthetic person"}),
                operation("conversations", conversation, {"profile_uid": profile, "title": "Synthetic"}),
                operation("messages", message, {"conversation_uid": conversation, "profile_uid": profile,
                    "role": "other", "text": "Hello", "confirmed": True, "observed_at": "2026-10-21T00:00:00Z", "order_key": "000001"}),
            ]}
            pushed = api.post("/v1/sync/push", json=push, headers=headers)
            assert pushed.status_code == 200
            assert all(result["status"] == "applied" for result in pushed.json()["results"])
            assert pushed.json()["results"][0]["op_id"] == push["operations"][0]["op_id"]
            for other_headers, _ in sessions[1:]:
                page = api.get("/v1/sync/pull", headers=other_headers).json()
                assert any(change["record"]["uid"] == message for change in page["changes"])
            body = {"request_id": str(uuid.uuid4()), "conversation_uid": conversation, "context_revision": 1, "kind": "analysis"}
            accepted = api.post("/v1/analyses", json=body, headers=headers)
            assert accepted.status_code == 202
            identifier = accepted.json()["analysis_id"]
            for _ in range(100):
                result = api.get("/v1/analyses/"+identifier, headers=sessions[1][0]).json()
                if result["status"] not in ("queued", "running"):
                    break
                time.sleep(0.05)
            assert result["status"] == "succeeded", result
            assert [row["text"] for row in result["result"]["candidates"]] == ["Second reply", "First reply"]
            assert len(provider_calls) == 3
            assert api.post("/v1/analyses", json=body, headers=headers).json()["analysis_id"] == identifier
            assert len(provider_calls) == 3
            page = api.get("/v1/sync/pull", headers=sessions[2][0]).json()
            assert any(change["record"]["entity_type"] == "analyses" and change["record"]["payload"]["status"] == "succeeded" for change in page["changes"])
            assert api.post("/v1/auth/logout", headers=sessions[1][0]).status_code == 200
            assert api.get("/v1/analyses/"+identifier, headers=sessions[1][0]).status_code == 401
    finally:
        PB.tearDownClass()


def test_actual_windows_repository_roundtrip_uses_real_gateway_and_pocketbase():
    package = types.ModuleType("windows_cloud_fixture")
    package.__path__ = [str(ROOT / "integrations/jev_windows/app")]
    sys.modules[package.__name__] = package
    from importlib import import_module
    Gateway = import_module("windows_cloud_fixture.gateway").GatewayClient
    Database = import_module("windows_cloud_fixture.local_db").LocalDatabase
    Repository = import_module("windows_cloud_fixture.repositories").CloudRepository
    spec = importlib.util.spec_from_file_location("pb_windows_fixture", ROOT / "services/pocketbase/tests/test_sync.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    PB = fixture.PocketBaseSyncTests
    bound = socket.socket()
    try:
        bound.bind(("127.0.0.1", 18000))
    except OSError:
        bound.close()
        pytest.skip("Development SSH tunnel port 18000 is already occupied; did not disturb it")
    bound.listen(128)
    PB.setUpClass()
    calls = []
    transport = httpx.AsyncHTTPTransport()
    async def route(request):
        if request.url.host == "127.0.0.1":
            return await transport.handle_async_request(request)
        calls.append(request)
        output = [{"facts": ["Confirmed"], "hypotheses": [], "unknowns": [], "strategy": "clarify"},
                  {"candidates": ["Reply A", "Reply B"]}, {"order": [1, 0]}][len(calls)-1]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(output)}, "finish_reason": "stop"}], "usage": {}})
    settings = Settings(operator_token="operator-"+"x"*48, pb_url=PB.url,
        routes={alias: Route("https://provider.example/v1", "fixture", "fixture-key") for alias in ("strategy", "draft")}, pb_internal_key=fixture.INTERNAL)
    server = uvicorn.Server(uvicorn.Config(create_app(settings, transport=httpx.MockTransport(route)),
        access_log=False, log_level="error"))
    thread = threading.Thread(target=lambda: server.run(sockets=[bound]), daemon=True)
    databases = []
    try:
        provision = PB.client.post("/internal/cloud/provision", headers={"X-Internal-Key": fixture.INTERNAL},
            json={"email": "windows@example.test", "password": fixture.PASSWORD})
        assert provision.status_code == 200
        thread.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        with tempfile.TemporaryDirectory(prefix="goutou-win-real-") as directory:
            first = Gateway("http://127.0.0.1:18000")
            session = first.login("windows@example.test", fixture.PASSWORD, "First Windows")
            db = Database(Path(directory)/"device-a", first.server, session["user"]["id"])
            databases.append(db)
            repo = Repository(db, first)
            profile = str(uuid.uuid4())
            db.save("profiles", profile, {"name": "Synthetic Windows profile", "stage": "初识", "goal": "自然接话", "background": "", "notes": ""})
            repo.bind_chat("wechat", "Synthetic window", profile, "device-a")
            result = repo.analyze_confirmed("wechat", "Synthetic window", [("her", "Hello"), ("me", "Hi")], "device-a")
            assert result["candidates"] == ["Reply B", "Reply A"]
            assert result["server_order"] is True
            assert len(calls) == 3
            second = Gateway("http://127.0.0.1:18000")
            session_b = second.login("windows@example.test", fixture.PASSWORD, "Second Windows")
            db_b = Database(Path(directory)/"device-b", second.server, session_b["user"]["id"])
            databases.append(db_b)
            Repository(db_b, second).syncer.sync()
            assert db_b.visible("profiles", profile)["payload"]["name"] == "Synthetic Windows profile"
            assert any(record["payload"]["status"] == "succeeded" for record in db_b.list_records("analyses"))
            for database in databases:
                database.close()
            databases.clear()
    finally:
        for database in databases:
            database.close()
        server.should_exit = True
        if thread.is_alive():
            thread.join(timeout=15)
        bound.close()
        PB.tearDownClass()
