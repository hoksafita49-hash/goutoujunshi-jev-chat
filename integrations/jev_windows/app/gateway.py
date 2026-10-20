"""Small strict client for the business gateway; never retries generation."""
from __future__ import annotations
import json
import urllib.error
import urllib.parse
import urllib.request


class CloudError(RuntimeError):
    def __init__(self, code, status=0):
        self.code, self.status = code, status
        super().__init__(f"云服务 {code}" + (f" (HTTP {status})" if status else ""))


def validate_server(value):
    value = str(value).strip().rstrip("/")
    parts = urllib.parse.urlsplit(value)
    try:
        port = parts.port
    except ValueError:
        raise ValueError("服务器地址格式不正确") from None
    local = parts.hostname in ("127.0.0.1", "localhost") and port == 18000
    if (not parts.hostname or parts.username or parts.password or "?" in value or "#" in value
            or parts.path not in ("", "/") or parts.scheme not in ("https", "http")
            or (parts.scheme == "http" and not local)):
        raise ValueError("请使用 HTTPS 服务器；开发隧道仅允许 http://127.0.0.1:18000")
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CloudError("redirect_rejected", code)


class GatewayClient:
    def __init__(self, server, session=None):
        self.server = validate_server(server)
        self.session = session
        self.opener = urllib.request.build_opener(_NoRedirect())

    def auth_headers(self):
        if not self.session:
            raise CloudError("login_required", 401)
        return {"Authorization": "Bearer " + self.session["token"],
                "X-Device-Session": self.session["device"]["id"],
                "X-Device-Secret": self.session["device_secret"]}

    def request(self, method, path, body=None, *, protected=True):
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if protected:
            headers.update(self.auth_headers())
        raw = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.server + path, raw, headers, method=method)
        try:
            with self.opener.open(req, timeout=30) as response:
                if response.status not in (200, 202):
                    raise CloudError("unexpected_status", response.status)
                payload = json.loads(response.read(8 * 1024 * 1024).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise CloudError("invalid_response")
                return payload
        except urllib.error.HTTPError as exc:
            try:
                code = json.loads(exc.read(65536)).get("error", {}).get("code", "request_failed")
            except (ValueError, AttributeError):
                code = "request_failed"
            raise CloudError(code, exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise CloudError("network_ambiguous") from None
        except (UnicodeError, ValueError):
            raise CloudError("invalid_response") from None

    def login(self, email, password, device_name):
        self.session = self.request("POST", "/v1/auth/login", dict(email=email, password=password,
                                    device_name=device_name, platform="windows"), protected=False)
        return self.session

    def refresh(self):
        self.session["token"] = self.request("POST", "/v1/auth/refresh")["token"]
        return self.session


def result_for_ui(result):
    rows = result.get("candidates") or []
    weights = [row.get("weight") if result.get("ranking_status") == "available" else None for row in rows]
    return {"candidates": [r["text"] for r in rows], "candidate_ids": [r["candidate_id"] for r in rows],
            "scores": weights, "best_index": 0 if rows else None, "server_order": True,
            "analysis_id": result["analysis_id"], "conversation_uid": result["conversation_uid"],
            "context_revision": result["context_revision"], "cloud_result": result,
            "goutou": {"facts": result.get("facts", []), "action": result.get("strategy", ""),
                       "intent": "；".join(result.get("hypotheses", [])) or "仍未知",
                       "unknown": "；".join(result.get("unknowns", [])),
                       "stop_condition": result.get("stop_condition", ""),
                       "next_step": "结合已核对原文选择候选，发送前由你确认。",
                       "evidence_note": "云端统一候选顺序；相对权重不是回复成功率。"}}
