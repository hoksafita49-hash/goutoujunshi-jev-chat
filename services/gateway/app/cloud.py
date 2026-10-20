import asyncio
from collections import deque
import json
import secrets
import time
from typing import Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .pb_client import PBClient, PBError
from .pipeline import PipelineError, object_from_text, run_pipeline

UID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=512)
    device_name: str = Field(min_length=1, max_length=100)
    platform: Literal["android", "ios", "windows"]


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op_id: str = Field(pattern=UID_PATTERN)
    entity_type: Literal["profiles", "conversations", "messages", "notes", "memories", "preferences", "identity_links", "feedback"]
    entity_uid: str = Field(pattern=UID_PATTERN)
    base_version: int = Field(ge=0)
    action: Literal["upsert", "delete"]
    payload: dict = Field(default_factory=dict)


class Push(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server_epoch: str = Field(pattern=UID_PATTERN)
    operations: list[Operation] = Field(max_length=50)


class Analysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern=UID_PATTERN)
    conversation_uid: str = Field(pattern=UID_PATTERN)
    context_revision: int = Field(ge=0)
    kind: Literal["analysis", "explanation", "rewrite"] = "analysis"
    parent_analysis_id: str | None = Field(default=None, pattern=UID_PATTERN)
    selected_text: str | None = Field(default=None, max_length=4096)
    preferences: dict | None = None


def permit(starts, maximum):
    now = time.monotonic()
    while starts and starts[0] <= now - 60:
        starts.popleft()
    if len(starts) >= maximum:
        raise PBError(429, "rate_limit_exceeded")
    starts.append(now)


def cloud_router():
    router = APIRouter()

    @router.post("/v1/auth/login")
    async def login(body: Login, request: Request):
        permit(request.app.state.login_starts, 20)
        return await request.app.state.pb.call("login", body.model_dump())

    @router.post("/v1/auth/refresh")
    async def refresh(request: Request):
        return await request.app.state.pb.call("refresh", session_headers=request.headers)

    @router.post("/v1/auth/logout")
    async def logout(request: Request):
        return await request.app.state.pb.call("logout", session_headers=request.headers)

    @router.get("/v1/devices")
    async def devices(request: Request):
        return await request.app.state.pb.call("devices", session_headers=request.headers)

    @router.delete("/v1/devices/{device_id}")
    async def revoke(device_id: str, request: Request):
        return await request.app.state.pb.call("revoke", {"id": device_id}, request.headers)

    @router.post("/v1/sync/push")
    async def push(body: Push, request: Request):
        return await request.app.state.pb.call("push", body.model_dump(mode="json"), request.headers)

    @router.get("/v1/sync/pull")
    async def pull(request: Request, cursor: str = "", limit: int = 200):
        if len(cursor) > 4096 or not 1 <= limit <= 200:
            raise PBError(422, "invalid_request")
        return await request.app.state.pb.call("pull", {"cursor": cursor, "limit": limit}, request.headers)

    @router.post("/v1/analyses", status_code=202)
    async def create(body: Analysis, request: Request):
        required = ("strategy", "draft") if body.kind == "analysis" else ("draft",)
        if any(not request.app.state.settings.routes.get(alias)
               or not request.app.state.settings.routes[alias].api_key for alias in required):
            raise PBError(503, "model_not_configured")
        permit(request.app.state.analysis_starts, 10)
        result = await request.app.state.pb.call("job-create", body.model_dump(mode="json", exclude_none=True), request.headers)
        return result

    @router.get("/v1/analyses/{analysis_id}")
    async def get(analysis_id: UUID, request: Request):
        return await request.app.state.pb.call("job-get", {"id": str(analysis_id)}, request.headers)

    @router.get("/v1/analyses/{analysis_id}/evidence")
    async def evidence(analysis_id: str, request: Request):
        import re
        if not re.fullmatch(UID_PATTERN, analysis_id):
            raise PBError(422, "invalid_request")
        return await request.app.state.pb.call("job-evidence", {"id": analysis_id}, request.headers)

    @router.get("/v1/memories/{memory_id}/history")
    async def memory_history(memory_id: str, request: Request, before_version: int = 0, limit: int = 50):
        import re
        if not re.fullmatch(UID_PATTERN, memory_id) or before_version < 0 or not 1 <= limit <= 50:
            raise PBError(422, "invalid_request")
        return await request.app.state.pb.call("memory-history", {"id": memory_id,
            "before_version": before_version, "limit": limit}, request.headers)

    return router


async def model_json(app, alias, messages, max_tokens, *, before_send=None):
    route = app.state.settings.routes.get(alias)
    if route is None or not route.api_key:
        raise PipelineError("model_not_configured")
    permit(app.state.starts, 30)
    payload = {"model": route.model, "messages": messages, "max_tokens": max_tokens,
        "temperature": 0.4, "stream": False, "response_format": {"type": "json_object"}}
    if route.disable_thinking:
        payload["thinking"] = {"type": "disabled"}
    async with app.state.inflight:
        if before_send is not None:
            await before_send()
        async with asyncio.timeout(95):
            async with app.state.upstream.stream("POST", route.base_url.rstrip("/") + "/chat/completions",
                json=payload, headers={"Authorization": "Bearer " + route.api_key}) as response:
                if response.status_code != 200:
                    raise PipelineError("upstream_error")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 2 * 1024 * 1024:
                        raise PipelineError("upstream_response_too_large")
    try:
        data = json.loads(raw)
        choice = data["choices"][0]
        if choice.get("finish_reason") not in (None, "stop"):
            raise PipelineError("incomplete_model_output")
        return object_from_text(choice["message"]["content"]), data.get("usage", {})
    except (KeyError, IndexError, TypeError, ValueError):
        raise PipelineError("invalid_model_output") from None


async def job_loop(app, stop):
    while not stop.is_set():
        claim = None
        try:
            # Reuse this token after a lost claim response. A new token could
            # strand the already committed job in running forever.
            claim_request = {"claim_token": secrets.token_urlsafe(48)}
            while True:
                try:
                    claim = await app.state.pb.call("job-claim", claim_request)
                    break
                except PBError as exc:
                    if exc.status < 500:
                        raise
                    await asyncio.sleep(2)
            job = claim.get("job")
            if job is None:
                await asyncio.wait_for(stop.wait(), timeout=2)
                continue
            try:
                async def check_claim():
                    await app.state.pb.call("job-check", {"analysis_id": job["analysis_id"],
                        "claim_token": claim["claim_token"]})

                result = await run_pipeline(job, lambda *args: model_json(app, *args, before_send=check_claim))
                finish = {"status": "succeeded", "result": result}
            except asyncio.CancelledError:
                await asyncio.shield(app.state.pb.call("job-finish", {"analysis_id": job["analysis_id"],
                    "claim_token": claim["claim_token"], "status": "indeterminate", "error_code": "gateway_stopped_outcome_unknown"}))
                raise
            except (httpx.TransportError, TimeoutError):
                finish = {"status": "indeterminate", "error_code": "upstream_outcome_unknown"}
            except (PipelineError, PBError) as exc:
                finish = {"status": "failed", "error_code": str(exc) if isinstance(exc, PipelineError) else exc.code}
            except Exception:
                finish = {"status": "failed", "error_code": "analysis_failed"}
            payload = dict(finish, analysis_id=job["analysis_id"], claim_token=claim["claim_token"])
            while True:
                try:
                    await app.state.pb.call("job-finish", payload)
                    break
                except PBError as exc:
                    if exc.status < 500:
                        break  # deleted/cancelled context or claim invalidated by recovery
                    # Retry only idempotent result persistence, never paid generation.
                    await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            continue
        except Exception:
            # A failed completion persistence is recovered as indeterminate on restart.
            # Never retry the model or create another job after an ambiguous outcome.
            try:
                await asyncio.wait_for(stop.wait(), timeout=2)
            except TimeoutError:
                pass


async def start_cloud(app, settings, upstream, enabled=True):
    app.state.settings = settings
    app.state.pb = PBClient(upstream, settings.pb_url, settings.pb_internal_key)
    app.state.login_starts = deque()
    app.state.analysis_starts = deque()
    app.state.cloud_stop = asyncio.Event()
    app.state.cloud_tasks = []
    if enabled and settings.pb_internal_key:
        await app.state.pb.call("job-recover")
        app.state.cloud_tasks = [asyncio.create_task(job_loop(app, app.state.cloud_stop)) for _ in range(2)]


async def stop_cloud(app):
    app.state.cloud_stop.set()
    for task in app.state.cloud_tasks:
        task.cancel()
    await asyncio.gather(*app.state.cloud_tasks, return_exceptions=True)
