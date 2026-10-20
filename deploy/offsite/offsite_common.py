"""Root-only offsite configuration and bounded, non-disclosing tool execution."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import tempfile


CONFIG_PATH = Path("/etc/goutou-offsite/config.json")
OFFSITE_LOCK_PATH = Path("/run/lock/goutou-offsite.lock")
MAX_JSON_BYTES = 1024 * 1024


class OffsiteError(Exception):
    """Only a fixed, non-secret machine code crosses the CLI boundary."""

    def __init__(self, code):
        self.code = code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else "offsite_error"
        super().__init__(self.code)


def require_root():
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise OffsiteError("root_required")


def ensure_private_regular(path, *, stat_fn=os.lstat):
    try:
        info = stat_fn(path)
    except OSError:
        raise OffsiteError("file_missing") from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise OffsiteError("file_not_private")


def ensure_private_directory(path):
    try:
        info = os.lstat(path)
    except OSError:
        raise OffsiteError("directory_missing") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise OffsiteError("directory_not_private")


def prepare_private_directory(path):
    """Only for a dedicated config/work directory, never a shared service parent."""
    path = Path(path)
    if path.is_symlink():
        raise OffsiteError("directory_not_private")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or (os.name == "posix" and info.st_uid != 0):
            raise OffsiteError("directory_not_private")
        path.chmod(0o700)
    except OSError:
        raise OffsiteError("directory_not_private") from None


def validate_config(config):
    if not isinstance(config, dict) or type(config.get("version")) is not int or config["version"] != 1:
        raise OffsiteError("config_invalid")
    if config.get("bucket") != "zovoeh-backup-20261021" or config.get("endpoint") != "https://oss-cn-beijing.aliyuncs.com" or config.get("prefix") != "goutou-v1":
        raise OffsiteError("config_invalid")
    for key in ("access_key_id", "access_key_secret", "password"):
        value = config.get(key)
        if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(char) < 32 for char in value):
            raise OffsiteError("config_invalid")
    if len(config["password"]) < 16:
        raise OffsiteError("config_invalid")
    return dict(config)


def load_config(path=CONFIG_PATH, *, file_check=ensure_private_regular):
    path = Path(path)
    if not path.exists():
        raise OffsiteError("config_missing")
    if file_check is ensure_private_regular:
        ensure_private_directory(path.parent)
    file_check(path)
    try:
        with path.open("rb") as handle:
            raw = handle.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise ValueError
        return validate_config(json.loads(raw))
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise OffsiteError("config_invalid") from None


def tool_env(config):
    config = validate_config(config)
    # Ambient alternate repository/password files or AWS credentials must not win.
    env = {key: value for key, value in os.environ.items() if not key.startswith(("RESTIC_", "RCLONE_", "AWS_"))}
    env.update({
        "RESTIC_REPOSITORY": f"rclone:goutouoss:{config['bucket']}/{config['prefix']}",
        "RESTIC_PASSWORD": config["password"],
        "RCLONE_CONFIG": os.devnull,
        "RCLONE_CONFIG_GOUTOUOSS_TYPE": "s3",
        "RCLONE_CONFIG_GOUTOUOSS_PROVIDER": "Alibaba",
        "RCLONE_CONFIG_GOUTOUOSS_ENDPOINT": config["endpoint"],
        "RCLONE_CONFIG_GOUTOUOSS_ACCESS_KEY_ID": config["access_key_id"],
        "RCLONE_CONFIG_GOUTOUOSS_SECRET_ACCESS_KEY": config["access_key_secret"],
        "RCLONE_CONFIG_GOUTOUOSS_NO_CHECK_BUCKET": "true",
        "RCLONE_CONFIG_GOUTOUOSS_ACL": "private",
        "RCLONE_RETRIES": "1",
        "RCLONE_LOW_LEVEL_RETRIES": "1",
        "RCLONE_CONTIMEOUT": "15s",
        "RCLONE_TIMEOUT": "60s",
    })
    return env


def run_tool(args, config, timeout=120):
    """Capture successful JSON, discard provider stderr, kill the whole tool group on timeout."""
    if not isinstance(timeout, (int, float)) or timeout <= 0 or timeout > 1800:
        raise OffsiteError("tool_timeout_invalid")
    try:
        process = subprocess.Popen(
            [str(arg) for arg in args], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=tool_env(config), text=True, encoding="utf-8", errors="replace",
            start_new_session=(os.name == "posix"),
        )
    except OSError:
        raise OffsiteError("tool_unavailable") from None
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise OffsiteError("tool_timeout") from None
    if process.returncode != 0:
        raise OffsiteError("tool_failed")
    return subprocess.CompletedProcess([str(arg) for arg in args], 0, stdout, "")


def atomic_json(path, data):
    """A mode-600 file is complete before it replaces the prior state/config."""
    path = Path(path)
    if path.is_symlink():
        raise OffsiteError("file_not_private")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    encoded = (json.dumps(data, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if len(encoded) > MAX_JSON_BYTES:
        raise OffsiteError("state_too_large")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".offsite-", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        if os.name == "posix":
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    except OSError:
        raise OffsiteError("state_write_failed") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class FileLock:
    """One nonblocking Linux flock, shared by setup, upload and recovery."""

    def __init__(self, path=OFFSITE_LOCK_PATH):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        try:
            import fcntl
        except ImportError:
            raise OffsiteError("platform_unsupported") from None
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            self.handle = os.fdopen(fd, "r+")
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
                raise OffsiteError("lock_invalid")
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return self
        except BlockingIOError:
            if self.handle is not None:
                self.handle.close()
                self.handle = None
            raise OffsiteError("busy") from None
        except (OSError, OffsiteError):
            if self.handle is not None:
                self.handle.close()
                self.handle = None
            raise OffsiteError("lock_invalid") from None

    def __exit__(self, *args):
        if self.handle is not None:
            self.handle.close()
            self.handle = None
