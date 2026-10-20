from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
import yaml
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from .pb_client import PBError
from .cloud import cloud_router, start_cloud, stop_cloud


@dataclass(frozen=True)
class Route:
    base_url: str
    model: str
    api_key: str
    disable_thinking: bool = False

    def __post_init__(self):
        parsed = urlsplit(self.base_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Model upstream must be a plain HTTPS base URL")
        if not self.model:
            raise ValueError("Model name is required")


@dataclass(frozen=True)
class Settings:
    operator_token: str
    pb_url: str
    routes: dict[str, Route]
    pb_internal_key: str = ""
    requests_per_minute: int = 10
    max_body_bytes: int = 256 * 1024
    body_timeout_seconds: float = 10

    def __post_init__(self):
        if len(self.operator_token) < 40 or not self.operator_token.isascii():
            raise ValueError("A strong ASCII operator token is required")
        private = urlsplit(self.pb_url)
        if (private.scheme != "http" or private.hostname != "127.0.0.1"
                or private.username or private.password or private.path or private.query or private.fragment):
            raise ValueError("PocketBase must use the private loopback endpoint")
        if self.pb_internal_key and len(self.pb_internal_key) < 40:
            raise ValueError("A strong internal service credential is required")
        if not 1 <= self.requests_per_minute <= 60:
            raise ValueError("Invalid rate limit")
        if not 1024 <= self.max_body_bytes <= 1024 * 1024:
            raise ValueError("Invalid body limit")
        if not 0 < self.body_timeout_seconds <= 30:
            raise ValueError("Invalid request receive timeout")
        if any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", alias) for alias in self.routes):
            raise ValueError("Invalid model alias")

    @classmethod
    def from_env(cls):
        path = Path(os.environ.get("GOUTOU_MODEL_CONFIG", "/etc/goutou/models.yaml"))
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(config, dict) or config.get("version") != 1:
            raise ValueError("Unsupported model configuration")
        routes = {}
        for alias, item in config["routes"].items():
            routes[alias] = Route(
                base_url=item["base_url"], model=item["model"],
                api_key=os.environ.get(item["api_key_env"], ""),
                disable_thinking=item.get("disable_thinking", False),
            )
        return cls(
            operator_token=os.environ["GOUTOU_OPERATOR_TOKEN"],
            pb_url="http://127.0.0.1:8090", routes=routes,
            pb_internal_key=os.environ.get("GOUTOU_PB_INTERNAL_KEY", ""),
        )


def error(status: int, code: str):
    return JSONResponse(
        {"error": {"code": code}},
        status_code=status, headers={"Cache-Control": "no-store"},
    )


class PrivateRequestBoundary:
    """Authenticate and bound the body before parsing it or calling a provider."""

    def __init__(self, app, settings: Settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope["path"].startswith("/v1/") and scope["path"] != "/v1/auth/login":
            headers = dict(scope["headers"])
            supplied = headers.get(b"authorization", b"")
            expected = ("Bearer " + self.settings.operator_token).encode("ascii")
            operator = secrets.compare_digest(supplied, expected)
            operator_paths = {"/v1/config", "/v1/models", "/v1/chat/completions"}
            if operator and scope["path"] not in operator_paths:
                return await error(403, "account_required")(scope, receive, send)
            if not operator:
                if not self.settings.pb_internal_key:
                    return await error(401, "unauthorized")(scope, receive, send)
                try:
                    account = await scope["app"].state.pb.authenticate({key.decode(): value.decode() for key, value in scope["headers"]})
                    scope.setdefault("state", {})["account"] = account
                except PBError as exc:
                    return await error(exc.status, exc.code)(scope, receive, send)
                if scope["path"] == "/v1/chat/completions":
                    return await error(403, "persistent_analysis_required")(scope, receive, send)
        body = bytearray()
        # Bound streamed/chunked bodies too, not just an untrusted Content-Length.
        try:
            async with asyncio.timeout(self.settings.body_timeout_seconds):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > self.settings.max_body_bytes:
                        return await error(413, "request_too_large")(scope, receive, send)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            return await error(408, "request_body_timeout")(scope, receive, send)
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        async def no_store(message):
            if message["type"] == "http.response.start" and scope["path"].startswith("/v1/"):
                message["headers"] = [(key, value) for key, value in message.get("headers", [])
                                      if key.lower() != b"cache-control"] + [(b"cache-control", b"no-store")]
            await send(message)
        await self.app(scope, replay, no_store)


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=32768)


class Completion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=32)
    messages: list[Message] = Field(min_length=1, max_length=64)
    stream: Literal[False] = False
    max_tokens: int = Field(default=1024, ge=1, le=4096)
    temperature: float = Field(default=0.7, ge=0, le=2)


def create_app(settings: Settings | None = None, *, transport=None, start_worker=True):
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(
            transport=transport, trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(90, connect=10),
        ) as upstream:
            app.state.upstream = upstream
            app.state.inflight = asyncio.Semaphore(2)
            app.state.starts = deque()
            await start_cloud(app, settings, upstream, enabled=start_worker)
            try:
                yield
            finally:
                await stop_cloud(app)

    app = FastAPI(
        title="Goutou private bootstrap gateway", lifespan=lifespan,
        docs_url=None, redoc_url=None, openapi_url=None,
    )
    app.add_middleware(PrivateRequestBoundary, settings=settings)
    app.include_router(cloud_router())

    @app.exception_handler(PBError)
    async def account_error(_request, exc):
        return error(exc.status, exc.code)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, _exc):
        # FastAPI's default detail includes the rejected input (possibly private text).
        return error(422, "invalid_request")

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready(request: Request):
        try:
            response = await request.app.state.upstream.get(
                settings.pb_url + "/api/health", timeout=5,
            )
            if response.status_code == 200 and response.json().get("code") == 200:
                return {"status": "ok"}
        except (httpx.HTTPError, ValueError, AttributeError):
            pass
        return error(503, "pocketbase_unavailable")

    @app.get("/v1/config")
    async def config(request: Request):
        account = getattr(request.state, "account", None)
        return {
            "api_version": 1, "min_client_version": 1,
            "stage": "private_cloud" if settings.pb_internal_key else "private_bootstrap",
            "sync_ready": bool(settings.pb_internal_key),
            "server_epoch": account.get("server_epoch") if account else None,
            "sync": {"push_limit": 50, "pull_limit": 200},
            "model_ready": any(route.api_key for route in settings.routes.values()),
            "supported": ["accounts", "sync", "analyses", "explanations", "rewrites"] if settings.pb_internal_key else ["text_chat_completions"],
        }

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": [
            {"id": alias, "object": "model", "owned_by": "goutou"}
            for alias, route in settings.routes.items() if route.api_key
        ]}

    @app.post("/v1/chat/completions")
    async def complete(body: Completion, request: Request):
        route = settings.routes.get(body.model)
        if route is None:
            return error(400, "unknown_model_alias")
        if not route.api_key:
            return error(503, "model_not_configured")
        state = request.app.state
        now = time.monotonic()
        while state.starts and state.starts[0] <= now - 60:
            state.starts.popleft()
        if len(state.starts) >= settings.requests_per_minute:
            return error(429, "rate_limit_exceeded")
        # No await between rate check and reservation: one process / one event loop.
        state.starts.append(now)
        try:
            await asyncio.wait_for(state.inflight.acquire(), timeout=0.1)
        except TimeoutError:
            return error(429, "gateway_busy")
        try:
            payload = body.model_dump()
            payload["model"] = route.model
            if route.disable_thinking:
                payload["thinking"] = {"type": "disabled"}
            # One attempt only. Timeouts can occur after a billable acceptance.
            async with asyncio.timeout(95):
                async with state.upstream.stream(
                    "POST", route.base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + route.api_key},
                    json=payload,
                ) as response:
                    if response.status_code != 200:
                        return error(502, "upstream_error")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 2 * 1024 * 1024:
                            return error(502, "upstream_response_too_large")
            data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get("choices"), list):
                return error(502, "invalid_upstream_response")
            safe = {key: data[key] for key in ("id", "object", "created", "choices", "usage") if key in data}
            safe["model"] = body.model
            return JSONResponse(safe, headers={"Cache-Control": "no-store"})
        except (httpx.TimeoutException, TimeoutError, httpx.TransportError):
            return error(504, "upstream_outcome_unknown")
        except (ValueError, UnicodeError):
            return error(502, "invalid_upstream_response")
        finally:
            state.inflight.release()

    return app
