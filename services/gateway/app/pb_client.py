from __future__ import annotations

import httpx


class PBError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


class PBClient:
    def __init__(self, client: httpx.AsyncClient, base_url: str, internal_key: str):
        self.client, self.base_url, self.internal_key = client, base_url, internal_key

    async def call(self, action: str, data=None, session_headers=None):
        headers = {"X-Internal-Key": self.internal_key}
        if session_headers:
            bearer = session_headers.get("authorization", "")
            headers.update({
                "X-Account-Token": bearer.removeprefix("Bearer "),
                "X-Device-Session": session_headers.get("x-device-session", ""),
                "X-Device-Secret": session_headers.get("x-device-secret", ""),
            })
        try:
            response = await self.client.post(self.base_url + "/internal/cloud/" + action,
                json=data or {}, headers=headers, timeout=12)
        except httpx.HTTPError:
            raise PBError(503, "account_service_unavailable") from None
        try:
            payload = response.json()
        except ValueError:
            raise PBError(503, "account_service_unavailable") from None
        if response.status_code >= 400:
            code = payload.get("error", {}).get("code", "account_service_unavailable")
            if not isinstance(code, str) or len(code) > 80:
                code = "account_service_unavailable"
            raise PBError(response.status_code if response.status_code < 500 else 503, code)
        return payload

    async def authenticate(self, headers):
        if (not headers.get("authorization", "").startswith("Bearer ")
                or not headers.get("x-device-session") or not headers.get("x-device-secret")):
            raise PBError(401, "unauthorized")
        return await self.call("auth", session_headers=headers)
