#!/usr/bin/env python3
"""Root-only offsite credentials setter. Secret input and tool output stay private."""
from __future__ import annotations

import argparse
import hmac
import json
from pathlib import Path
import re
import shutil
import sys

from offsite_common import (
    CONFIG_PATH, OFFSITE_LOCK_PATH, FileLock, OffsiteError, atomic_json,
    load_config, prepare_private_directory, require_root, run_tool, validate_config,
)

BUCKET = "zovoeh-backup-20261021"
ENDPOINT = "https://oss-cn-beijing.aliyuncs.com"
PREFIX = "goutou-v1"
MAX_INPUT = 16 * 1024
SAFE_CODES = frozenset({
    "invalid_input", "requires_root", "root_required", "busy", "maintenance_busy",
    "tool_unavailable", "tool_timeout", "cloud_probe_failed",
    "repository_open_failed", "repository_init_failed", "repository_missing", "password_change_forbidden",
    "config_invalid", "config_missing", "unsafe_config", "configuration_failed",
})


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def parse_input(raw: bytes) -> dict:
    """Accept only the three credentials; the remote target is fixed server-side."""
    try:
        if not isinstance(raw, bytes) or not raw or len(raw) > MAX_INPUT:
            raise ValueError("input size")
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(data, dict) or set(data) != {"access_key_id", "access_key_secret", "password"}:
            raise ValueError("input fields")
        key_id, secret, password = (data[name] for name in ("access_key_id", "access_key_secret", "password"))
        if not isinstance(key_id, str) or re.fullmatch(r"[A-Za-z0-9]{8,128}", key_id) is None:
            raise ValueError("key id")
        if not isinstance(secret, str) or re.fullmatch(r"[A-Za-z0-9+/=_\-]{16,256}", secret) is None:
            raise ValueError("secret")
        if not isinstance(password, str) or not 16 <= len(password) <= 4096 or any(c in password for c in "\r\n\0"):
            raise ValueError("password")
        if password == secret or password == key_id:
            raise ValueError("independent password required")
        return validate_config({"version": 1, "bucket": BUCKET, "endpoint": ENDPOINT, "prefix": PREFIX, **data})
    except (ValueError, TypeError, UnicodeError, RecursionError, OffsiteError):
        raise OffsiteError("invalid_input") from None


def _checked_tool(args: list[str], config: dict, runner, failure: str):
    try:
        result = runner(args, config, timeout=90)
    except OffsiteError as error:
        if error.code in {"tool_unavailable", "tool_timeout"}:
            raise
        raise OffsiteError(failure) from None
    if result.returncode != 0:
        raise OffsiteError(failure)
    return result


def configure_credentials(config_path: Path, candidate: dict, *, runner=run_tool,
                          lock_path: Path = OFFSITE_LOCK_PATH) -> dict:
    """Validate storage and password before atomically replacing local credentials."""
    config_path = Path(config_path)
    candidate = validate_config(candidate)
    if candidate["bucket"] != BUCKET or candidate["endpoint"] != ENDPOINT or candidate["prefix"] != PREFIX:
        raise OffsiteError("invalid_input")
    with FileLock(lock_path):
        previously_configured = config_path.exists() or config_path.is_symlink()
        if previously_configured:
            previous = load_config(config_path)
            if not hmac.compare_digest(previous["password"].encode("utf-8"), candidate["password"].encode("utf-8")):
                raise OffsiteError("password_change_forbidden")
        listing = _checked_tool(
            ["rclone", "lsf", f"goutouoss:{BUCKET}/{PREFIX}", "--recursive"],
            candidate, runner, "cloud_probe_failed",
        )
        if listing.stdout.strip():
            _checked_tool(["restic", "cat", "config"], candidate, runner, "repository_open_failed")
        else:
            if previously_configured:
                raise OffsiteError("repository_missing")
            # Only a successful recursive listing proves that initialization is safe.
            _checked_tool(["restic", "init"], candidate, runner, "repository_init_failed")
        prepare_private_directory(config_path.parent)
        atomic_json(config_path, candidate)
    return {"status": "configured", "bucket": BUCKET, "endpoint": ENDPOINT, "prefix": PREFIX}


def check_status(config_path: Path = CONFIG_PATH) -> dict:
    """Read local readiness only. Do not probe storage, initialize, or upload."""
    tools = {name: shutil.which(name) is not None for name in ("restic", "rclone")}
    result = {"status": "ready_for_credentials", "configured": False, "tools_available": all(tools.values()),
              "tools": tools, "bucket": BUCKET, "endpoint": ENDPOINT, "prefix": PREFIX}
    if Path(config_path).exists() or Path(config_path).is_symlink():
        load_config(config_path)
        result["configured"] = True
    if not result["tools_available"]:
        result["code"] = "tool_unavailable"
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Configure encrypted OSS backups using private stdin JSON.")
    parser.add_argument("--check", action="store_true", help="Read local readiness without contacting OSS.")
    args = parser.parse_args(argv)
    try:
        require_root()
        if args.check:
            result = check_status()
        else:
            candidate = parse_input(sys.stdin.buffer.read(MAX_INPUT + 1))
            result = configure_credentials(CONFIG_PATH, candidate)
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except OffsiteError as error:
        code = error.code if error.code in SAFE_CODES else "configuration_failed"
    except Exception:
        code = "configuration_failed"
    # Neither exception strings, subprocess output nor tracebacks cross this boundary.
    print(json.dumps({"status": "error", "code": code}, separators=(",", ":")))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
