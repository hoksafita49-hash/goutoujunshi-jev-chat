#!/usr/bin/env python3
"""Receive a DeepSeek key over SSH stdin, validate, and activate without echoing it."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ENV_PATH = Path("/etc/goutou/gateway.env")
KEY_NAME = "DEEPSEEK_API_KEY"


class ConfigurationError(Exception):
    pass


def validate_key(key):
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9._~+/:=@!%\-]{8,4096}", key):
        raise ValueError("invalid_key_format")


def parse_input(raw):
    if len(raw) > 8192:
        raise ValueError("invalid_input")
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {"key"}:
        raise ValueError("invalid_input")
    validate_key(value["key"])
    return value["key"]


def render_environment(original, key):
    validate_key(key)
    lines = original.decode("utf-8").splitlines(keepends=True)
    indexes = [i for i, line in enumerate(lines) if re.match(r"\s*DEEPSEEK_API_KEY\s*=", line)]
    if len(indexes) != 1:
        raise ValueError("ambiguous_existing_configuration")
    lines[indexes[0]] = KEY_NAME + "=" + key + "\n"
    return "".join(lines).encode("utf-8")


def atomic_write(path, content):
    descriptor, name = tempfile.mkstemp(prefix=".gateway-env-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as file:
            os.chmod(temporary, 0o600)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def configure_key(path, key, activate, verify=None):
    original = path.read_bytes()
    replacement = render_environment(original, key)
    atomic_write(path, replacement)
    try:
        if not activate():
            raise ConfigurationError("activation_failed")
        if verify is not None and not verify():
            raise ConfigurationError("alias_verification_failed")
    except Exception:
        atomic_write(path, original)
        try:
            restored = activate()
        except Exception:
            restored = False
        raise ConfigurationError("activation_failed_previous_config_restored" if restored
                                 else "configuration_restored_service_needs_attention") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get_json(url, token=None):
    # Fixed destinations only; do not forward a bearer on redirects or via proxy env vars.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    headers = {} if token is None else {"Authorization": "Bearer " + token}
    request = urllib.request.Request(url, headers=headers)
    with opener.open(request, timeout=12) as response:
        raw = response.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise ConfigurationError("response_too_large")
        return json.loads(raw)


def verify_provider(key, required_models):
    try:
        response = get_json("https://api.deepseek.com/v1/models", key)
        available = {item["id"] for item in response["data"]}
        if not required_models.issubset(available):
            raise ConfigurationError("configured_model_not_available")
    except urllib.error.HTTPError as exc:
        raise ConfigurationError("provider_auth_rejected" if exc.code in (401, 403)
                                 else "provider_check_failed") from None
    except ConfigurationError:
        raise
    except Exception:
        raise ConfigurationError("provider_check_unavailable") from None


def operator_token():
    for line in ENV_PATH.read_text().splitlines():
        if line.startswith("GOUTOU_OPERATOR_TOKEN="):
            return line.partition("=")[2].strip()
    raise ConfigurationError("operator_configuration_missing")


def restart_gateway():
    result = subprocess.run(["systemctl", "restart", "goutou-gateway"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=35)
    if result.returncode:
        return False
    for _ in range(30):
        try:
            if get_json("http://127.0.0.1:8000/health/ready").get("status") == "ok":
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main():
    import fcntl
    import yaml
    if os.geteuid() != 0:
        raise ConfigurationError("root_required")
    if sys.argv[1:] == ["--check"]:
        config = get_json("http://127.0.0.1:8000/v1/config", operator_token())
        print(json.dumps({"status": "ready_for_key", "configured": config["model_ready"]}))
        return
    if sys.argv[1:]:
        raise ConfigurationError("invalid_arguments")
    key = parse_input(sys.stdin.buffer.read(8193))
    with open("/run/lock/goutou-maintenance.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigurationError("maintenance_busy") from None
        config = yaml.safe_load(Path("/etc/goutou/models.yaml").read_text())
        routes = {alias: item for alias, item in config["routes"].items()
                  if item["api_key_env"] == KEY_NAME}
        if not routes or any(item["base_url"] != "https://api.deepseek.com/v1" for item in routes.values()):
            raise ConfigurationError("unexpected_provider_configuration")
        if ENV_PATH.is_symlink() or not ENV_PATH.is_file() or ENV_PATH.stat().st_uid != 0:
            raise ConfigurationError("unexpected_environment_file")
        if ENV_PATH.stat().st_mode & 0o777 != 0o600:
            raise ConfigurationError("unexpected_environment_permissions")
        verify_provider(key, {item["model"] for item in routes.values()})
        def verify_aliases():
            ready = get_json("http://127.0.0.1:8000/v1/models", operator_token())
            return set(routes).issubset({item["id"] for item in ready["data"]})
        configure_key(ENV_PATH, key, restart_gateway, verify=verify_aliases)
        print(json.dumps({"status": "configured", "provider_auth": "ok",
                          "aliases": sorted(routes), "generation_tested": False}))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, ConfigurationError) as exc:
        code = str(exc) if isinstance(exc, ConfigurationError) else "invalid_input"
        print(json.dumps({"status": "error", "code": code}))
        sys.exit(1)
    except Exception:
        # Never print an exception object, response, or payload that could carry a key.
        print(json.dumps({"status": "error", "code": "configuration_failed"}))
        sys.exit(1)
