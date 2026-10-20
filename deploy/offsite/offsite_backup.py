#!/usr/bin/env python3
"""Encrypt a completed cold archive and its matching offline runtime using restic."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import tempfile

from offsite_common import (
    CONFIG_PATH, OFFSITE_LOCK_PATH, MAX_JSON_BYTES, FileLock, OffsiteError,
    atomic_json, ensure_private_regular, load_config, prepare_private_directory,
    require_root, run_tool, validate_config,
)


ARCHIVE_PATTERN = re.compile(r"goutou-(\d{8}T\d{6}Z)\.tar\.gz\Z")
SHA_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
MAX_STAGE_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_EXPANDED = 4 * 1024 * 1024 * 1024
TAG = "goutou-production-v1"
HOST = "goutou-private-vps"
SKIP_DIRECTORIES = {"venv", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", ".git"}


@dataclass(frozen=True)
class Paths:
    backup_dir: Path = Path("/var/backups/goutou")
    releases_dir: Path = Path("/opt/goutou/releases")
    pocketbase_dir: Path = Path("/opt/goutou/pocketbase-0.40.5")
    wheels_dir: Path = Path("/var/lib/goutou/offsite-runtime/wheels")
    caddy_binary: Path = Path("/usr/bin/caddy")
    systemd_dirs: tuple[Path, ...] = (Path("/etc/systemd/system"), Path("/lib/systemd/system"), Path("/usr/lib/systemd/system"))
    work_dir: Path = Path("/var/lib/goutou/offsite-work")
    state_file: Path = Path("/var/lib/goutou/offsite-state.json")
    maintenance_lock: Path = Path("/run/lock/goutou-maintenance.lock")
    offsite_lock: Path = OFFSITE_LOCK_PATH


def _timestamp(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _regular(path, *, missing="runtime_missing"):
    try:
        info = path.lstat()
    except OSError:
        raise OffsiteError(missing) from None
    if not stat.S_ISREG(info.st_mode):
        raise OffsiteError(missing)
    return info


def _open_regular(path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise OffsiteError("runtime_missing")
        return os.fdopen(fd, "rb")
    except OSError:
        raise OffsiteError("runtime_missing") from None


def _sha256(path):
    digest = hashlib.sha256()
    with _open_regular(path) as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path, code):
    try:
        with _open_regular(path) as handle:
            raw = handle.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise ValueError
        return json.loads(raw)
    except (OSError, ValueError, UnicodeError, RecursionError, OffsiteError):
        raise OffsiteError(code) from None


class BackupEngine:
    def __init__(self, config, *, paths=None, runner=run_tool, lock_factory=FileLock,
                 now=None, root_check=require_root, file_check=ensure_private_regular,
                 max_bytes=MAX_STAGE_BYTES):
        self.config = validate_config(config)
        self.paths = paths or Paths()
        self.runner = runner
        self.lock_factory = lock_factory
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.root_check = root_check
        self.file_check = file_check
        self.max_bytes = min(max_bytes, MAX_STAGE_BYTES)

    def _load_state(self):
        path = self.paths.state_file
        if not path.exists():
            return None
        self.file_check(path)
        data = _read_json(path, "state_invalid")
        try:
            if not isinstance(data, dict) or not ARCHIVE_PATTERN.fullmatch(data["archive_name"]):
                raise ValueError
            if not SHA_PATTERN.fullmatch(data["digest"]) or not SHA_PATTERN.fullmatch(data["snapshot_id"]):
                raise ValueError
            when = datetime.strptime(data["time"], "%Y-%m-%dT%H:%M:%SZ")
            history = data.get("completed", {data["archive_name"]: data["digest"]})
            dates = data.get("successful_dates", [when.strftime("%Y-%m-%d")])
            if not isinstance(history, dict) or not isinstance(dates, list):
                raise ValueError
            if any(not ARCHIVE_PATTERN.fullmatch(name) or not SHA_PATTERN.fullmatch(digest) for name, digest in history.items()):
                raise ValueError
            for day in dates:
                datetime.strptime(day, "%Y-%m-%d")
            return {**data, "completed": history, "successful_dates": dates}
        except (KeyError, TypeError, ValueError):
            raise OffsiteError("state_invalid") from None

    def check(self):
        self.root_check()
        state = self._load_state()
        metadata = {key: state[key] for key in ("archive_name", "digest", "snapshot_id", "time")} if state else None
        return {"status": "ready", "configured": True, "latest_success": metadata}

    def _latest(self, now):
        try:
            names = sorted((path for path in self.paths.backup_dir.iterdir() if ARCHIVE_PATTERN.fullmatch(path.name)), key=lambda path: path.name, reverse=True)
        except OSError:
            raise OffsiteError("archive_missing") from None
        if not names:
            raise OffsiteError("archive_missing")
        latest = names[0]
        self.file_check(latest)
        try:
            stamp = datetime.strptime(ARCHIVE_PATTERN.fullmatch(latest.name)[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            raise OffsiteError("archive_invalid") from None
        age = now - stamp
        if age < timedelta(0) or age > timedelta(hours=30):
            raise OffsiteError("archive_stale")
        if latest.stat().st_size > self.max_bytes:
            raise OffsiteError("stage_too_large")
        return latest

    def _validate_archive(self, path):
        manifest = None
        seen = set()
        expanded = 0
        allowed = ("var/lib/goutou/pocketbase", "etc/goutou", "etc/caddy", "var/lib/caddy")
        try:
            with gzip.open(path, "rb") as compressed:
                with tarfile.open(fileobj=compressed, mode="r|") as archive:
                    for member in archive:
                        name = member.name.rstrip("/")
                        parts = name.split("/")
                        if not name or "\\" in name or name.startswith("/") or any(part in ("", ".", "..") or ":" in part for part in parts):
                            raise OffsiteError("archive_unsafe")
                        if name in seen or not (member.isfile() or member.isdir()):
                            raise OffsiteError("archive_unsafe")
                        seen.add(name)
                        if name != "opt/goutou/deployment.json" and not any(name == root or name.startswith(root + "/") for root in allowed):
                            raise OffsiteError("archive_unsafe")
                        if member.isfile():
                            expanded += member.size
                            if expanded > MAX_ARCHIVE_EXPANDED:
                                raise OffsiteError("archive_too_large")
                            source = archive.extractfile(member)
                            if name == "opt/goutou/deployment.json":
                                if member.size > 64 * 1024:
                                    raise OffsiteError("archive_invalid")
                                manifest = json.loads(source.read())
                            else:
                                while source.read(1024 * 1024):
                                    pass
                        if compressed.tell() > MAX_ARCHIVE_EXPANDED:
                            raise OffsiteError("archive_too_large")
                # tar EOF precedes gzip EOF; consume the trailer to verify its CRC.
                while compressed.read(1024 * 1024):
                    if compressed.tell() > MAX_ARCHIVE_EXPANDED:
                        raise OffsiteError("archive_too_large")
        except OffsiteError:
            raise
        except (OSError, EOFError, tarfile.TarError, ValueError, UnicodeError, RecursionError):
            raise OffsiteError("archive_invalid") from None
        if not isinstance(manifest, dict) or manifest.get("pocketbase") != "0.40.5":
            raise OffsiteError("archive_invalid")
        return self._release(manifest.get("gateway_release"))

    def _release(self, value):
        if not isinstance(value, str) or ".." in PurePosixPath(value.replace("\\", "/")).parts:
            raise OffsiteError("release_invalid")
        release = Path(value)
        try:
            if not self.paths.releases_dir.is_dir():
                raise ValueError
            # Windows fixture sandbox denies strict directory handles; POSIX production
            # resolves every component strictly before checking the release boundary.
            root = self.paths.releases_dir.resolve(strict=(os.name == "posix"))
            if not release.is_absolute() or release.is_symlink() or not release.is_dir():
                raise ValueError
            relative = release.resolve(strict=(os.name == "posix")).relative_to(root)
            if len(relative.parts) != 1:
                raise ValueError
            _regular(release / ".complete")
            services = release / "services"
            if services.is_symlink() or not services.is_dir():
                raise ValueError
            return release
        except (OSError, ValueError, OffsiteError):
            raise OffsiteError("release_invalid") from None

    def _stage(self, bundle, archive, now):
        files = {}
        total = 0

        def copy(source, relative):
            nonlocal total
            info = _regular(source)
            if total + info.st_size > self.max_bytes:
                raise OffsiteError("stage_too_large")
            destination = bundle / relative
            destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            digest = hashlib.sha256()
            with _open_regular(source) as reader, destination.open("xb") as writer:
                destination.chmod(0o600)
                for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise OffsiteError("stage_too_large")
                    writer.write(chunk)
                    digest.update(chunk)
            files[relative] = digest.hexdigest()
            return digest.hexdigest()

        archive_digest = copy(archive, "snapshot.tar.gz")
        release = self._validate_archive(bundle / "snapshot.tar.gz")
        services = release / "services"
        mandatory = [
            "gateway/app/main.py", "gateway/requirements.lock",
            "pocketbase/pb_hooks/main.pb.js", "pocketbase/pb_hooks/cloud.js",
            "pocketbase/pb_hooks/evidence_memory.js",
        ]
        for name in mandatory:
            _regular(services / name)
        for name in ("pocketbase/pb_hooks", "pocketbase/pb_migrations"):
            directory = services / name
            if directory.is_symlink() or not directory.is_dir() or not any(path.is_file() and not path.is_symlink() for path in directory.rglob("*")):
                raise OffsiteError("runtime_missing")
        for directory, dirs, filenames in os.walk(services, followlinks=False):
            dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRECTORIES)
            for name in dirs:
                if (Path(directory) / name).is_symlink():
                    raise OffsiteError("runtime_missing")
            for name in sorted(filenames):
                if name.endswith((".pyc", ".pyo")):
                    continue
                path = Path(directory) / name
                copy(path, "runtime/services/" + path.relative_to(services).as_posix())
        copy(self.paths.pocketbase_dir / "pocketbase", "runtime/pocketbase/pocketbase")
        licenses = [path for path in self.paths.pocketbase_dir.iterdir() if path.name.upper().startswith("LICENSE") and path.is_file() and not path.is_symlink()]
        if not licenses:
            raise OffsiteError("runtime_missing")
        for path in sorted(licenses):
            copy(path, "runtime/pocketbase/" + path.name)
        self._stage_wheels(copy, services / "gateway/requirements.lock")
        copy(self.paths.caddy_binary, "runtime/caddy/caddy")
        copied_units = set()
        for directory in self.paths.systemd_dirs:
            candidates = sorted(directory.glob("goutou-*.service")) + [directory / "caddy.service"]
            for unit in candidates:
                if unit.name in copied_units or unit.is_symlink() or not unit.is_file():
                    continue
                copy(unit, "runtime/systemd/" + unit.name)
                copied_units.add(unit.name)
        manifest = {
            "schema": "goutou-offsite-bundle-v1", "archive_name": archive.name,
            "created_at": _timestamp(now), "pocketbase_version": "0.40.5",
            "source_release": release.name, "python_version": "3.12", "files": files,
        }
        encoded = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
        if total + len(encoded) > self.max_bytes:
            raise OffsiteError("stage_too_large")
        target = bundle / "bundle-manifest.json"
        target.write_bytes(encoded)
        target.chmod(0o600)
        return archive_digest

    def _stage_wheels(self, copy, requirements):
        directory = self.paths.wheels_dir
        if directory.is_symlink() or not directory.is_dir():
            raise OffsiteError("runtime_missing")
        checksum_path = directory.parent / "requirements.lock.sha256"
        inventory_path = directory.parent / "WHEELS.json"
        _regular(checksum_path)
        _regular(inventory_path)
        try:
            checksum = checksum_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            raise OffsiteError("runtime_mismatch") from None
        if not SHA_PATTERN.fullmatch(checksum) or checksum != _sha256(requirements):
            raise OffsiteError("runtime_mismatch")
        inventory = _read_json(inventory_path, "runtime_mismatch")
        wheels = sorted(directory.glob("*.whl"))
        if not wheels:
            raise OffsiteError("runtime_missing")
        if not isinstance(inventory, dict) or set(inventory) != {path.name for path in wheels}:
            raise OffsiteError("runtime_mismatch")
        wheel_versions = set()
        for wheel in wheels:
            _regular(wheel)
            parts = wheel.name[:-4].split("-")
            if len(parts) < 5:
                raise OffsiteError("runtime_mismatch")
            wheel_versions.add((re.sub(r"[-_.]+", "-", parts[0]).lower(), parts[1]))
            digest = copy(wheel, "runtime/wheels/" + wheel.name)
            if inventory[wheel.name] != digest:
                raise OffsiteError("runtime_mismatch")
        try:
            for line in requirements.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!-]+)", line)
                if not match or (re.sub(r"[-_.]+", "-", match[1]).lower(), match[2]) not in wheel_versions:
                    raise OffsiteError("runtime_mismatch")
        except (OSError, UnicodeError):
            raise OffsiteError("runtime_mismatch") from None
        copy(checksum_path, "runtime/requirements.lock.sha256")
        copy(inventory_path, "runtime/WHEELS.json")

    def _new_state(self, old, archive, digest, snapshot, now):
        history = dict(old["completed"]) if old else {}
        history[archive.name] = digest
        dates = sorted(set((old["successful_dates"] if old else []) + [now.strftime("%Y-%m-%d")]))
        state = {"archive_name": archive.name, "digest": digest, "snapshot_id": snapshot,
                 "time": _timestamp(now), "completed": history, "successful_dates": dates}
        if len(json.dumps(state, indent=2).encode()) + 1 > MAX_JSON_BYTES:
            raise OffsiteError("state_too_large")
        return state

    def run(self, *, force=False):
        self.root_check()
        now = self.now().astimezone(timezone.utc)
        with self.lock_factory(self.paths.offsite_lock), ExitStack() as cleanup:
            state = self._load_state()
            with self.lock_factory(self.paths.maintenance_lock):
                archive = self._latest(now)
                digest = _sha256(archive)
                history = state["completed"] if state else {}
                if archive.name in history or digest in history.values():
                    return {"status": "skipped", "code": "already_uploaded"}
                if not force and state and now.strftime("%Y-%m-%d") in state["successful_dates"]:
                    return {"status": "skipped", "code": "daily_limit"}
                # Check durable ledger capacity before any possible successful upload.
                self._new_state(state, archive, digest, "0" * 64, now)
                prepare_private_directory(self.paths.work_dir)
                temporary = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="capture-", dir=self.paths.work_dir))
                bundle = Path(temporary).resolve() / "bundle"
                bundle.mkdir(mode=0o700)
                if self._stage(bundle, archive, now) != digest:
                    raise OffsiteError("archive_changed")
            result = self.runner([
                "restic", "backup", str(bundle), "--tag", TAG, "--host", HOST,
                "--json", "--limit-upload", "256",
            ], self.config, timeout=1500)
            snapshot = None
            try:
                for line in result.stdout.splitlines():
                    entry = json.loads(line)
                    if isinstance(entry, dict) and entry.get("message_type") == "summary":
                        snapshot = entry.get("snapshot_id")
                if not isinstance(snapshot, str) or not SHA_PATTERN.fullmatch(snapshot):
                    raise ValueError
            except (ValueError, TypeError, RecursionError):
                raise OffsiteError("backup_result_invalid") from None
            completed_at = self.now().astimezone(timezone.utc)
            atomic_json(self.paths.state_file, self._new_state(state, archive, digest, snapshot, completed_at))
            try:
                self.runner([
                    "restic", "forget", "--tag", TAG, "--host", HOST,
                    "--group-by", "host,tags", "--keep-daily", "7", "--keep-weekly", "4",
                    "--keep-monthly", "3", "--prune",
                    "--limit-upload", "256", "--max-repack-size", "64M",
                ], self.config, timeout=900)
            except OffsiteError:
                return {"status": "degraded", "code": "retention_failed", "snapshot_id": snapshot}
            return {"status": "uploaded", "snapshot_id": snapshot}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Upload the latest completed cold archive as an encrypted offsite snapshot.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="Read-only configuration and last-success metadata; no network.")
    group.add_argument("--force", action="store_true", help="Root verification: bypass daily success limit; deduplication still applies.")
    options = parser.parse_args(argv)
    try:
        require_root()
        try:
            config = load_config(CONFIG_PATH)
        except OffsiteError as exc:
            if options.check and exc.code == "config_missing":
                print(json.dumps({"status": "not_configured", "configured": False, "latest_success": None}))
                return 0
            raise
        engine = BackupEngine(config)
        result = engine.check() if options.check else engine.run(force=options.force)
        print(json.dumps(result, sort_keys=True))
        return 2 if result["status"] == "degraded" else 0
    except OffsiteError as exc:
        print(json.dumps({"status": "error", "code": exc.code}))
        return 1
    except (OSError, ValueError, TypeError):
        print(json.dumps({"status": "error", "code": "local_operation_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
