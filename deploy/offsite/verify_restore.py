#!/usr/bin/env python3
"""Prove an offline bundle restores inside a dedicated, loopback-only netns.

Run as root using: unshare --net sh -c 'ip link set lo up; exec python3
verify_restore.py --bundle /private/restored/bundle'. This tool never manages
system services. Child output is discarded because it may contain private data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


PB_URL = "http://127.0.0.1:18091"
GATEWAY_URL = "http://127.0.0.1:18092"
SCHEMA = "goutou-offsite-bundle-v1"


class VerificationError(Exception):
    """Only constant, non-sensitive error codes may enter the public report."""


def require_isolated_namespace(*, platform=None, euid=None, readlink=None, interfaces=None):
    platform = sys.platform if platform is None else platform
    euid = getattr(os, "geteuid", lambda: -1)() if euid is None else euid
    readlink = os.readlink if readlink is None else readlink
    interfaces = socket.if_nameindex if interfaces is None else interfaces
    if platform != "linux" or euid != 0:
        raise VerificationError("linux_root_required")
    try:
        current, initial = readlink("/proc/self/ns/net"), readlink("/proc/1/ns/net")
        names = {name for _, name in interfaces()}
    except (OSError, ValueError):
        raise VerificationError("network_isolation_unproven") from None
    if not current or not initial or current == initial or names != {"lo"}:
        raise VerificationError("network_isolation_unproven")


def relative_path(value, code="unsafe_bundle_path"):
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
            or "\x00" in value or value.startswith("/")
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise VerificationError(code)
    return PurePosixPath(value)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def validate_bundle(bundle):
    bundle = Path(bundle)
    if bundle.is_symlink() or not bundle.is_dir():
        raise VerificationError("bundle_manifest_invalid")
    manifest_path = bundle / "bundle-manifest.json"
    if manifest_path.is_symlink():
        raise VerificationError("bundle_symlink_rejected")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    except (OSError, UnicodeError, ValueError):
        raise VerificationError("bundle_manifest_invalid") from None
    if (not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA
            or manifest.get("pocketbase_version") != "0.40.5"
            or manifest.get("python_version") != "3.12" or not isinstance(manifest.get("files"), dict)):
        raise VerificationError("bundle_manifest_invalid")
    files = manifest["files"]
    for name, digest in files.items():
        relative_path(name)
        if name == "bundle-manifest.json" or not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
            raise VerificationError("bundle_manifest_invalid")
    actual = set()
    for path in bundle.rglob("*"):
        if path.is_symlink():
            raise VerificationError("bundle_symlink_rejected")
        if path.is_file():
            actual.add(path.relative_to(bundle).as_posix())
        elif not path.is_dir():
            raise VerificationError("bundle_special_file_rejected")
    if actual - {"bundle-manifest.json"} != set(files):
        raise VerificationError("bundle_file_set_mismatch")
    required = {"snapshot.tar.gz", "runtime/pocketbase/pocketbase", "runtime/caddy/caddy",
                "runtime/services/gateway/app/main.py", "runtime/services/gateway/requirements.lock",
                "runtime/services/pocketbase/pb_hooks/main.pb.js"}
    if not required <= set(files) or not all(any(name.startswith(prefix) for name in files)
            for prefix in ("runtime/wheels/", "runtime/services/pocketbase/pb_migrations/")):
        raise VerificationError("bundle_runtime_missing")
    for name, digest in files.items():
        if sha256_file(bundle / name) != digest:
            raise VerificationError("bundle_hash_mismatch")
    return manifest


def stage_bundle(bundle, destination, manifest):
    destination.mkdir(mode=0o700)
    for name, digest in manifest["files"].items():
        source, target = bundle / name, destination / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Recheck copied bytes before extraction/execution, closing copy races.
        shutil.copyfile(source, target, follow_symlinks=False)
        if target.is_symlink() or sha256_file(target) != digest:
            raise VerificationError("bundle_hash_mismatch")
        target.chmod(0o600)
    return destination


def snapshot_path_allowed(name):
    return name == "opt/goutou/deployment.json" or any(name == prefix or name.startswith(prefix + "/")
        for prefix in ("etc/goutou", "etc/caddy", "var/lib/caddy", "var/lib/goutou/pocketbase"))


def extract_snapshot(snapshot, destination):
    destination = Path(destination)
    if destination.is_symlink() or not destination.is_dir() or any(destination.iterdir()):
        raise VerificationError("restore_root_not_empty")
    try:
        with tarfile.open(snapshot, "r:gz") as archive:
            members, seen = archive.getmembers(), set()
            for member in members:
                name = member.name.rstrip("/") if member.isdir() else member.name
                relative_path(name, "unsafe_snapshot_member")
                if not snapshot_path_allowed(name) or not (member.isfile() or member.isdir()) or name in seen:
                    raise VerificationError("unsafe_snapshot_member")
                seen.add(name)
            # A file cannot be the parent of another member (including reversed order).
            files = {member.name for member in members if member.isfile()}
            if any(str(parent) in files for name in seen for parent in PurePosixPath(name).parents):
                raise VerificationError("unsafe_snapshot_member")
            expected = {}
            for member in members:
                if member.isfile():
                    digest = hashlib.sha256()
                    with archive.extractfile(member) as file:
                        for block in iter(lambda: file.read(1024 * 1024), b""):
                            digest.update(block)
                    expected[member.name] = digest.hexdigest()
            archive.extractall(path=destination, members=members, filter="data")
    except (tarfile.TarError, OSError, ValueError):
        raise VerificationError("snapshot_extract_failed") from None
    extracted = {path.relative_to(destination).as_posix(): sha256_file(path)
                 for path in destination.rglob("*") if path.is_file()}
    if extracted != expected:
        raise VerificationError("snapshot_file_hash_mismatch")
    return extracted


def inspect_database(path):
    try:
        with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            if [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
                raise VerificationError("sqlite_integrity_failed")
            tables = ("cloud_users", "cloud_entities", "cloud_changes", "cloud_jobs")
            records = {table: [dict(row) for row in connection.execute("SELECT * FROM " + table)] for table in tables}
    except (sqlite3.Error, OSError):
        raise VerificationError("sqlite_integrity_failed") from None
    return {"counts": {table: len(rows) for table, rows in records.items()}, "records": records}


def stop_child(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


class Children:
    def __init__(self):
        self.processes = []

    def __enter__(self):
        return self

    def start(self, command, **kwargs):
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, **kwargs)
        self.processes.append(process)
        return process

    def check(self):
        if any(process.poll() is not None for process in self.processes):
            raise VerificationError("restored_service_exited")

    def __exit__(self, *_):
        for process in reversed(self.processes):
            stop_child(process)


def run_private(command, *, timeout=120, **kwargs):
    with Children() as children:
        process = children.start(command, **kwargs)
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise VerificationError("offline_command_timeout") from None
        if returncode:
            raise VerificationError("offline_command_failed")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class LocalAPI:
    def __init__(self, base):
        if base not in (PB_URL, GATEWAY_URL):
            raise VerificationError("unsafe_restore_endpoint")
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(self, path, *, data=None, headers=None, status=200):
        if not path.startswith("/") or path.startswith("//") or "\r" in path or "\n" in path:
            raise VerificationError("unsafe_restore_endpoint")
        request = urllib.request.Request(self.base + path, headers={"Content-Type": "application/json", **(headers or {})},
            data=None if data is None else json.dumps(data).encode("utf-8"))
        try:
            response = self.opener.open(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        except (OSError, urllib.error.URLError):
            raise VerificationError("restored_api_unavailable") from None
        with response:
            if response.status != status:
                raise VerificationError("restored_api_status_failed")
            try:
                result = json.loads(response.read(8 * 1024 * 1024 + 1))
            except (ValueError, UnicodeError):
                raise VerificationError("restored_api_invalid_json") from None
        if not isinstance(result, dict):
            raise VerificationError("restored_api_invalid_json")
        return result


def wait_ready(api, path, children, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        children.check()
        try:
            api.call(path)
            children.check()
            return
        except VerificationError:
            time.sleep(.1)
    raise VerificationError("restored_service_start_timeout")


def session_headers(session, *, internal=False):
    return {"X-Account-Token" if internal else "Authorization": session["token"] if internal else "Bearer " + session["token"],
            "X-Device-Session": session["device"]["id"], "X-Device-Secret": session["device_secret"]}


def object_json(value):
    return json.loads(value or "null") if isinstance(value, str) else value


def public_entity(row):
    return {key: (object_json(row[key]) or {}) if key == "payload" else (row[key] or None) if key == "deleted_at" else row[key]
            for key in ("entity_type", "uid", "version", "created_at", "updated_at", "deleted_at", "payload")}


def verify_account_data(api, session, frozen):
    """Compare normal-user API responses with the frozen DB; never return PII."""
    owner, headers = session["user"]["id"], session_headers(session)
    entities = {(row["entity_type"], row["uid"]): row for row in frozen["records"]["cloud_entities"] if row["owner"] == owner}
    jobs = [row for row in frozen["records"]["cloud_jobs"] if row["owner"] == owner]
    recovering = {row["uid"] for row in jobs if row["status"] in ("queued", "running")}
    latest, cursor, change_count, seen_cursors = {}, "", 0, set()
    while True:
        page = api.call("/v1/sync/pull?limit=200&cursor=" + urllib.parse.quote(cursor, safe=""), headers=headers)
        if page.get("server_epoch") != session["server_epoch"] or not isinstance(page.get("changes"), list):
            raise VerificationError("sync_preservation_failed")
        for change in page["changes"]:
            record = change["record"]
            latest[(record["entity_type"], record["uid"])] = record
            change_count += 1
        if not page.get("has_more"):
            break
        cursor = page.get("cursor")
        if not cursor or cursor in seen_cursors:
            raise VerificationError("sync_pagination_failed")
        seen_cursors.add(cursor)
    if set(latest) != set(entities):
        raise VerificationError("sync_preservation_failed")
    for key, row in entities.items():
        if key[0] == "analyses" and key[1] in recovering:
            if latest[key].get("payload", {}).get("status") != "indeterminate":
                raise VerificationError("queued_job_recovery_failed")
        elif latest[key] != public_entity(row):
            raise VerificationError("sync_preservation_failed")
    memory_count = history_count = evidence_count = 0
    for (kind, uid), row in entities.items():
        if kind != "memories" or row["deleted_at"]:
            continue
        memory_count += 1
        expected = sorted([object_json(change["record"]) for change in frozen["records"]["cloud_changes"]
            if change["owner"] == owner and change["entity_type"] == "memories" and change["uid"] == uid],
            key=lambda value: value["version"], reverse=True)
        versions, before = [], 0
        while True:
            page = api.call(f"/v1/memories/{uid}/history?limit=50&before_version={before}", headers=headers)
            versions.extend(page["versions"])
            if not page.get("has_more"):
                break
            following = page.get("next_before_version")
            if not isinstance(following, int) or following <= 0 or (before and following >= before):
                raise VerificationError("history_preservation_failed")
            before = following
        if versions != expected:
            raise VerificationError("history_preservation_failed")
        history_count += len(versions)
    for job in jobs:
        restored = api.call("/v1/analyses/" + job["uid"], headers=headers)
        wanted_status = "indeterminate" if job["uid"] in recovering else job["status"]
        if restored.get("status") != wanted_status or restored.get("result") != object_json(job.get("result")):
            raise VerificationError("job_preservation_failed")
        conversation = entities.get(("conversations", job["conversation_uid"]))
        if job["status"] == "cancelled" or conversation is None or conversation["deleted_at"]:
            api.call("/v1/analyses/" + job["uid"] + "/evidence", headers=headers, status=404)
            continue
        evidence = api.call("/v1/analyses/" + job["uid"] + "/evidence", headers=headers)
        source = object_json(job["input"]) or {}
        result = object_json(job.get("result")) or {}
        messages = []
        for message in source.get("messages", []):
            current = entities.get(("messages", message["uid"]))
            if current and not current["deleted_at"] and object_json(current["payload"]).get("profile_uid") == source.get("profile_uid"):
                messages.append(message)
        if evidence.get("messages") != messages or evidence.get("evidence") != result.get("evidence", []):
            raise VerificationError("evidence_preservation_failed")
        evidence_count += 1
    return {"owner_entities": len(entities), "owner_changes": change_count, "memories_checked": memory_count,
            "history_versions_checked": history_count, "evidence_records_checked": evidence_count,
            "jobs_checked": len(jobs), "interrupted_jobs_invalidated": len(recovering)}


def verify_certificates(restored):
    certificates = list((restored / "var/lib/caddy").rglob("*.crt"))
    try:
        for certificate in certificates:
            key = certificate.with_suffix(".key")
            if not key.is_file():
                raise VerificationError("certificate_keypair_invalid")
            ssl._ssl._test_decode_cert(str(certificate))
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(certificate, key)
    except (ssl.SSLError, OSError, ValueError):
        raise VerificationError("certificate_keypair_invalid") from None
    return len(certificates)


GATEWAY_BOOTSTRAP = """import os
import uvicorn
from app.main import Settings, create_app
settings = Settings(operator_token=os.environ['GOUTOU_OPERATOR_TOKEN'],
    pb_url='http://127.0.0.1:18091', routes={},
    pb_internal_key=os.environ['GOUTOU_PB_INTERNAL_KEY'])
uvicorn.run(create_app(settings, start_worker=False), host='127.0.0.1',
    port=18092, access_log=False, log_level='critical')
"""


def verify_restore(bundle):
    require_isolated_namespace()
    if sys.version_info[:2] != (3, 12):
        raise VerificationError("python_312_required")
    bundle = Path(bundle).absolute()
    manifest = validate_bundle(bundle)
    # Bind-test both isolated ports before any login/reset. A pre-existing listener
    # must never be mistaken for our copied PocketBase or gateway.
    reservations = []
    try:
        for port in (18091, 18092):
            reservation = socket.socket()
            reservations.append(reservation)
            reservation.bind(("127.0.0.1", port))
    except OSError:
        raise VerificationError("restore_port_in_use") from None
    finally:
        for reservation in reservations:
            reservation.close()
    with tempfile.TemporaryDirectory(prefix="goutou-offline-restore-") as directory:
        private = Path(directory)
        private.chmod(0o700)
        staged = stage_bundle(bundle, private / "bundle", manifest)
        restored = private / "restored"
        restored.mkdir(mode=0o700)
        file_hashes = extract_snapshot(staged / "snapshot.tar.gz", restored)
        required = {"var/lib/goutou/pocketbase/data.db", "etc/goutou/application-user.json",
                    "etc/goutou/pocketbase.env", "etc/goutou/gateway.env", "etc/goutou/models.yaml",
                    "opt/goutou/deployment.json"}
        if not required <= set(file_hashes):
            raise VerificationError("snapshot_required_file_missing")
        frozen = inspect_database(restored / "var/lib/goutou/pocketbase/data.db")
        certificates = verify_certificates(restored)
        credentials = json.loads((restored / "etc/goutou/application-user.json").read_text())
        original_env = dict(line.split("=", 1) for line in (restored / "etc/goutou/pocketbase.env").read_text().splitlines()
                            if line and not line.startswith("#") and "=" in line)
        internal_key = original_env["GOUTOU_PB_INTERNAL_KEY"]
        if len(internal_key) < 40:
            raise VerificationError("restored_credentials_invalid")
        environment = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "HOME": str(private),
                       "TMPDIR": str(private), "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        venv = private / "venv"
        run_private([sys.executable, "-m", "venv", str(venv)], env=environment, cwd=private)
        python = venv / "bin/python"
        run_private([str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-cache-dir",
            "--no-index", "--find-links", str(staged / "runtime/wheels"), "-r",
            str(staged / "runtime/services/gateway/requirements.lock")], env=environment, cwd=private)
        run_private([str(python), "-m", "pip", "check"], env=environment, cwd=private)
        binary = staged / "runtime/pocketbase/pocketbase"
        binary.chmod(0o700)
        safe_models = private / "offline-models.yaml"
        safe_models.write_text("version: 1\nroutes: {}\n")
        environment.update(GOUTOU_PB_INTERNAL_KEY=internal_key,
            GOUTOU_OPERATOR_TOKEN=secrets.token_urlsafe(48), GOUTOU_MODEL_CONFIG=str(safe_models),
            PYTHONPATH=str(staged / "runtime/services/gateway"))
        pb, gateway = LocalAPI(PB_URL), LocalAPI(GATEWAY_URL)
        with Children() as children:
            children.start([str(binary), "serve", "--http=127.0.0.1:18091",
                "--dir=" + str(restored / "var/lib/goutou/pocketbase"),
                "--hooksDir=" + str(staged / "runtime/services/pocketbase/pb_hooks"),
                "--migrationsDir=" + str(staged / "runtime/services/pocketbase/pb_migrations"),
                "--automigrate=false", "--hooksWatch=false"], env=environment, cwd=private)
            wait_ready(pb, "/api/health", children)
            internal = {"X-Internal-Key": internal_key}
            login = {"email": credentials["email"], "password": credentials["password"],
                     "device_name": "offline-restore-before-reset", "platform": "windows"}
            prior = pb.call("/internal/cloud/login", data=login, headers=internal)
            children.check()
            rotated = pb.call("/internal/cloud/reset-epoch", data={}, headers=internal)
            if not rotated.get("server_epoch") or rotated["server_epoch"] == prior["server_epoch"]:
                raise VerificationError("epoch_rotation_failed")
            pb.call("/internal/cloud/auth", data={}, headers={**internal, **session_headers(prior, internal=True)}, status=401)
            children.start([str(python), "-c", GATEWAY_BOOTSTRAP], env=environment, cwd=private)
            wait_ready(gateway, "/health/ready", children)
            gateway.call("/v1/config", headers=session_headers(prior), status=401)
            fresh = gateway.call("/v1/auth/login", data={**login, "device_name": "offline-restore-fresh"})
            if fresh.get("server_epoch") != rotated["server_epoch"]:
                raise VerificationError("owner_login_failed")
            api_counts = verify_account_data(gateway, fresh, frozen)
            children.check()
            after = inspect_database(restored / "var/lib/goutou/pocketbase/data.db")
            if any(row["status"] in ("queued", "running") for row in after["records"]["cloud_jobs"]):
                raise VerificationError("queued_job_recovery_failed")
        # Original bundle must remain a byte-for-byte source after exercising only
        # the private copy. These hashes never enter the report.
        validate_bundle(bundle)
        return {"ok": True, "schema": "goutou-offsite-restore-report-v1", "network_isolated": True,
            "manifest_verified": True, "restored_file_hashes_verified": True, "sqlite_integrity_ok": True,
            "offline_venv_verified": True, "owner_login_ok": True, "epoch_rotated": True,
            "old_session_rejected": True, "gateway_pull_verified": True,
            "snapshot_files": len(file_hashes), "bundle_files": len(manifest["files"]),
            "database_counts": frozen["counts"], **api_counts,
            "certificate_keypairs_checked": certificates, "tls_listener_tested": False,
            "model_requests_tested": False, "workers_disabled": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = verify_restore(args.bundle)
    except VerificationError as error:
        # Codes are created internally; never serialize an upstream exception.
        report = {"ok": False, "error": str(error) if re.fullmatch("[a-z_]{1,80}", str(error)) else "restore_verification_failed"}
    except (Exception, KeyboardInterrupt):
        report = {"ok": False, "error": "restore_verification_failed"}
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
