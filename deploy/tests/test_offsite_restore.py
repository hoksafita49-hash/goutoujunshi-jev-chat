"""Offline restore verification must never reach a production namespace or path."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import time

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "offsite/verify_restore.py"


@pytest.fixture
def verifier():
    # An explicit assertion makes the first RED run describe the missing feature.
    assert SCRIPT.exists(), "The isolated restore verifier has not been implemented"
    spec = importlib.util.spec_from_file_location("offsite_restore", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def bundle_fixture(tmp_path):
    bundle = tmp_path / "bundle"
    content = {
        "snapshot.tar.gz": b"snapshot-placeholder",
        "runtime/pocketbase/pocketbase": b"copied-pb",
        "runtime/caddy/caddy": b"copied-caddy",
        "runtime/services/gateway/app/main.py": b"# copied gateway\n",
        "runtime/services/gateway/requirements.lock": b"fixture==1\n",
        "runtime/services/pocketbase/pb_hooks/main.pb.js": b"// hooks\n",
        "runtime/services/pocketbase/pb_migrations/1.js": b"// migration\n",
        "runtime/wheels/fixture-1-py3-none-any.whl": b"wheel",
        "runtime/systemd/goutou-gateway.service": b"service",
    }
    for name, data in content.items():
        path = bundle / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = {"schema": "goutou-offsite-bundle-v1", "pocketbase_version": "0.40.5",
                "python_version": "3.12", "files": {
                    name: hashlib.sha256(data).hexdigest() for name, data in content.items()}}
    (bundle / "bundle-manifest.json").write_text(json.dumps(manifest))
    return bundle, manifest


def write_tar(path, members):
    with tarfile.open(path, "w:gz") as archive:
        for name, body, kind in members:
            item = tarfile.TarInfo(name)
            item.type = kind
            item.size = len(body) if kind == tarfile.REGTYPE else 0
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                item.linkname = "/etc/goutou/pocketbase.env"
            archive.addfile(item, io.BytesIO(body) if item.isfile() else None)


def test_requires_root_and_a_dedicated_loopback_only_network_namespace(verifier):
    def namespaces(path):
        return "net:[1]" if str(path) == "/proc/1/ns/net" else "net:[2]"

    assert verifier.require_isolated_namespace(platform="linux", euid=0,
        readlink=namespaces, interfaces=lambda: [(1, "lo")]) is None
    cases = [
        dict(platform="win32", euid=0, readlink=namespaces, interfaces=lambda: [(1, "lo")]),
        dict(platform="linux", euid=1000, readlink=namespaces, interfaces=lambda: [(1, "lo")]),
        dict(platform="linux", euid=0, readlink=lambda _: "net:[1]", interfaces=lambda: [(1, "lo")]),
        dict(platform="linux", euid=0, readlink=namespaces, interfaces=lambda: [(1, "lo"), (2, "eth0")]),
        dict(platform="linux", euid=0, readlink=namespaces, interfaces=lambda: []),
    ]
    for case in cases:
        with pytest.raises(verifier.VerificationError):
            verifier.require_isolated_namespace(**case)


def test_manifest_covers_every_file_and_detects_changes_before_staging(verifier, tmp_path):
    bundle, _ = bundle_fixture(tmp_path)
    assert len(verifier.validate_bundle(bundle)["files"]) == 9
    (bundle / "runtime/pocketbase/pocketbase").write_bytes(b"tampered")
    with pytest.raises(verifier.VerificationError, match="bundle_hash_mismatch"):
        verifier.validate_bundle(bundle)


@pytest.mark.parametrize("bad_path", ["../escape", "/etc/passwd", "runtime/../escape", "runtime\\evil", "C:/evil", "runtime//evil"])
def test_manifest_rejects_unsafe_paths(verifier, tmp_path, bad_path):
    bundle, manifest = bundle_fixture(tmp_path)
    manifest["files"][bad_path] = "a" * 64
    (bundle / "bundle-manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(verifier.VerificationError, match="unsafe_bundle_path"):
        verifier.validate_bundle(bundle)


def test_manifest_missing_file_or_unlisted_file_is_rejected(verifier, tmp_path):
    bundle, _ = bundle_fixture(tmp_path)
    (bundle / "runtime/wheels/fixture-1-py3-none-any.whl").unlink()
    with pytest.raises(verifier.VerificationError, match="bundle_file_set_mismatch"):
        verifier.validate_bundle(bundle)
    (bundle / "bundle-manifest.json").unlink()
    with pytest.raises(verifier.VerificationError, match="bundle_manifest_invalid"):
        verifier.validate_bundle(bundle)


def test_bundle_without_optional_systemd_units_can_still_restore(verifier, tmp_path):
    bundle, manifest = bundle_fixture(tmp_path)
    name = "runtime/systemd/goutou-gateway.service"
    (bundle / name).unlink()
    del manifest["files"][name]
    (bundle / "bundle-manifest.json").write_text(json.dumps(manifest))
    assert len(verifier.validate_bundle(bundle)["files"]) == 8


def test_symlink_inside_bundle_is_rejected(verifier, tmp_path):
    bundle, _ = bundle_fixture(tmp_path)
    path = bundle / "runtime/linked"
    try:
        path.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires Windows developer mode")
    with pytest.raises(verifier.VerificationError, match="bundle_symlink_rejected"):
        verifier.validate_bundle(bundle)


@pytest.mark.parametrize("name,kind", [
    ("../escape", tarfile.REGTYPE), ("/etc/passwd", tarfile.REGTYPE),
    ("etc/goutou/../../escape", tarfile.REGTYPE),
    ("etc/goutou/link", tarfile.SYMTYPE), ("etc/goutou/link", tarfile.LNKTYPE),
    ("etc/goutou/pipe", tarfile.FIFOTYPE), ("opt/goutou/current/evil", tarfile.REGTYPE),
])
def test_archive_rejects_traversal_links_special_files_and_live_runtime(verifier, tmp_path, name, kind):
    snapshot = tmp_path / "snapshot.tar.gz"
    write_tar(snapshot, [("etc/goutou/safe", b"private", tarfile.REGTYPE), (name, b"evil", kind)])
    destination = tmp_path / "restored"
    destination.mkdir()
    with pytest.raises(verifier.VerificationError, match="unsafe_snapshot_member"):
        verifier.extract_snapshot(snapshot, destination)
    assert not list(destination.rglob("*")), "Validation must finish before any extraction"
    assert not (tmp_path / "escape").exists()


def test_archive_restores_only_beneath_new_root_with_exact_file_bytes(verifier, tmp_path, monkeypatch):
    if sys.platform == "win32":
        # Windows AppContainer denies the patched stdlib's strict ALLOW_MISSING
        # realpath even on writable roots. Keep its actual data filter and only
        # adapt that Windows path-resolution operation; the Linux CLI is guarded.
        realpath = os.path.realpath
        monkeypatch.setattr(os.path, "realpath", lambda path, *, strict=False: realpath(path, strict=False))
    snapshot = tmp_path / "snapshot.tar.gz"
    write_tar(snapshot, [("etc/goutou/pocketbase.env", b"secret=fixture\n", tarfile.REGTYPE),
                         ("var/lib/goutou/pocketbase/data.db", b"copied-db", tarfile.REGTYPE)])
    destination = tmp_path / "new-private-root"
    destination.mkdir()
    result = verifier.extract_snapshot(snapshot, destination)
    assert (destination / "var/lib/goutou/pocketbase/data.db").read_bytes() == b"copied-db"
    assert result == {"etc/goutou/pocketbase.env": hashlib.sha256(b"secret=fixture\n").hexdigest(),
                      "var/lib/goutou/pocketbase/data.db": hashlib.sha256(b"copied-db").hexdigest()}


def test_sqlite_is_checked_read_only_and_corruption_is_rejected(verifier, tmp_path):
    db = tmp_path / "data.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE cloud_users (id TEXT, email TEXT)")
        connection.execute("INSERT INTO cloud_users VALUES ('fixture-owner','private@example.test')")
        connection.execute("CREATE TABLE cloud_entities (owner TEXT,entity_type TEXT,uid TEXT,version INT,created_at TEXT,updated_at TEXT,deleted_at TEXT,payload TEXT)")
        connection.execute("CREATE TABLE cloud_changes (owner TEXT,entity_type TEXT,uid TEXT,version INT,seq INT,record TEXT)")
        connection.execute("CREATE TABLE cloud_jobs (owner TEXT,uid TEXT,status TEXT,conversation_uid TEXT,input TEXT)")
    before = db.read_bytes()
    checked = verifier.inspect_database(db)
    assert checked["counts"]["cloud_users"] == 1
    assert db.read_bytes() == before
    db.write_bytes(b"not-sqlite-private-fixture")
    with pytest.raises(verifier.VerificationError, match="sqlite_integrity_failed"):
        verifier.inspect_database(db)


def test_child_processes_are_terminated_when_verification_raises(verifier):
    process = None
    with pytest.raises(verifier.VerificationError):
        with verifier.Children() as children:
            process = children.start([sys.executable, "-c", "import time; time.sleep(60)"])
            raise verifier.VerificationError("fixture_failed")
    assert process is not None and process.poll() is not None


def test_command_timeout_stops_process_and_never_exposes_output(verifier):
    started = time.monotonic()
    with pytest.raises(verifier.VerificationError, match="offline_command_timeout") as caught:
        verifier.run_private([sys.executable, "-c", "import time; print('password-private-fixture',flush=True); time.sleep(60)"], timeout=.1)
    assert time.monotonic() - started < 10
    assert "password-private-fixture" not in str(caught.value)


def test_cli_failure_returns_only_safe_code_without_secret_or_traceback(verifier, capsys, monkeypatch):
    def fail(_):
        raise RuntimeError("password-private-fixture")
    monkeypatch.setattr(verifier, "verify_restore", fail)
    assert verifier.main(["--bundle", "private-fixture"]) == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {"ok": False, "error": "restore_verification_failed"}
    assert "password-private-fixture" not in output.out + output.err
    assert "Traceback" not in output.out + output.err


def test_http_client_rejects_any_non_restore_endpoint(verifier):
    for endpoint in ("http://127.0.0.1:8090", "https://example.com", "http://localhost:18091", "http://127.0.0.1:18091@evil.test"):
        with pytest.raises(verifier.VerificationError, match="unsafe_restore_endpoint"):
            verifier.LocalAPI(endpoint)


def test_host_namespace_rejection_happens_before_any_bundle_access(verifier, monkeypatch):
    def reject():
        raise verifier.VerificationError("network_isolation_unproven")
    def read_bundle(_):
        pytest.fail("A rejected host invocation accessed the bundle")
    monkeypatch.setattr(verifier, "require_isolated_namespace", reject)
    monkeypatch.setattr(verifier, "validate_bundle", read_bundle)
    with pytest.raises(verifier.VerificationError, match="network_isolation_unproven"):
        verifier.verify_restore(Path("/private/credentials/bundle"))


def test_child_cleanup_escalates_to_kill_for_a_stuck_service(verifier):
    class UnresponsiveChild:
        alive = True
        def poll(self):
            return None if self.alive else -9
        def terminate(self):
            pass
        def kill(self):
            self.alive = False
        def wait(self, timeout):
            if self.alive:
                raise subprocess.TimeoutExpired("private command", timeout)
            return -9
    process = UnresponsiveChild()
    verifier.stop_child(process)
    assert process.poll() == -9


def test_normal_user_pull_must_preserve_each_entity_and_payload(verifier):
    expected = {"entity_type": "profiles", "uid": "fixture-profile", "version": 2,
                "created_at": "date", "updated_at": "date", "deleted_at": None,
                "payload": {"private": "fixture-text"}}
    row = {**expected, "owner": "owner", "deleted_at": "", "payload": json.dumps(expected["payload"])}
    frozen = {"records": {"cloud_entities": [row], "cloud_jobs": [], "cloud_changes": []}}
    session = {"token": "private-token", "device_secret": "private-device-secret", "device": {"id": "device"},
               "user": {"id": "owner"}, "server_epoch": "epoch"}
    class API:
        record = expected
        def call(self, path, **_kwargs):
            assert path == "/v1/sync/pull?limit=200&cursor="
            return {"server_epoch": "epoch", "changes": [{"seq": 1, "record": self.record}], "has_more": False}
    api = API()
    counts = verifier.verify_account_data(api, session, frozen)
    assert counts["owner_entities"] == 1
    api.record = {**expected, "payload": {"private": "lost"}}
    with pytest.raises(verifier.VerificationError, match="sync_preservation_failed"):
        verifier.verify_account_data(api, session, frozen)


def test_archive_hashes_are_compared_with_source_members_after_extraction(verifier, tmp_path, monkeypatch):
    snapshot = tmp_path / "snapshot.tar.gz"
    write_tar(snapshot, [("etc/goutou/pocketbase.env", b"original-secret", tarfile.REGTYPE)])
    destination = tmp_path / "restored"
    destination.mkdir()
    def corrupt(self, *args, **kwargs):
        # Simulate filesystem/storage corruption after extraction without relying
        # on the sandbox-restricted Windows stdlib path resolver.
        (destination / "etc/goutou").mkdir(parents=True)
        (destination / "etc/goutou/pocketbase.env").write_bytes(b"corrupted-secret")
    monkeypatch.setattr(tarfile.TarFile, "extractall", corrupt)
    with pytest.raises(verifier.VerificationError, match="snapshot_file_hash_mismatch"):
        verifier.extract_snapshot(snapshot, destination)


def test_blank_pocketbase_json_fields_are_read_like_its_hooks(verifier):
    assert verifier.object_json("") is None


def test_completed_analysis_result_must_survive_normal_user_read(verifier):
    session = {"token": "private-token", "device_secret": "private-secret", "device": {"id": "device"},
               "user": {"id": "owner"}, "server_epoch": "epoch"}
    job = {"owner": "owner", "uid": "analysis", "status": "succeeded", "conversation_uid": "deleted-conversation",
           "input": "{}", "result": '{"private":"preserved-result"}'}
    frozen = {"records": {"cloud_entities": [], "cloud_jobs": [job], "cloud_changes": []}}
    class API:
        def call(self, path, **kwargs):
            if path.startswith("/v1/sync/pull"):
                return {"server_epoch": "epoch", "changes": [], "has_more": False}
            if path.endswith("/evidence"):
                assert kwargs["status"] == 404
                return {"error": {"code": "not_found"}}
            assert path == "/v1/analyses/analysis"
            return {"status": "succeeded", "result": {"private": "lost-result"}}
    with pytest.raises(verifier.VerificationError, match="job_preservation_failed"):
        verifier.verify_account_data(API(), session, frozen)


def test_memory_history_and_evidence_are_compared_with_frozen_private_records(verifier):
    session = {"token": "private-token", "device_secret": "private-secret", "device": {"id": "device"},
               "user": {"id": "owner"}, "server_epoch": "epoch"}
    records = [
        {"entity_type": "conversations", "uid": "conversation", "version": 1, "created_at": "date", "updated_at": "date",
         "deleted_at": None, "payload": {"profile_uid": "profile"}},
        {"entity_type": "messages", "uid": "message", "version": 1, "created_at": "date", "updated_at": "date",
         "deleted_at": None, "payload": {"profile_uid": "profile", "text": "private-text"}},
        {"entity_type": "memories", "uid": "memory", "version": 2, "created_at": "date", "updated_at": "date",
         "deleted_at": None, "payload": {"profile_uid": "profile", "text": "private-memory"}},
    ]
    message = {"uid": "message", "profile_uid": "profile", "text": "private-text"}
    history = [{**records[2], "version": 1, "payload": {"profile_uid": "profile", "text": "private-old-memory"}}, records[2]]
    job = {"owner": "owner", "uid": "analysis", "status": "succeeded", "conversation_uid": "conversation",
           "input": json.dumps({"profile_uid": "profile", "messages": [message]}), "result": '{"evidence":[{"id":"e1"}]}'}
    frozen = {"records": {
        "cloud_entities": [{**row, "owner": "owner", "deleted_at": "", "payload": json.dumps(row["payload"])} for row in records],
        "cloud_jobs": [job],
        "cloud_changes": [{"owner": "owner", "entity_type": "memories", "uid": "memory", "record": json.dumps(row)} for row in history],
    }}
    class API:
        lose_history = False
        lose_evidence = False
        def call(self, path, **kwargs):
            if path.startswith("/v1/sync/pull"):
                return {"server_epoch": "epoch", "changes": [{"seq": i+1, "record": row} for i, row in enumerate(records)], "has_more": False}
            if path.startswith("/v1/memories/"):
                return {"versions": [] if self.lose_history else list(reversed(history)), "has_more": False}
            if path.endswith("/evidence"):
                return {"messages": [message], "evidence": [] if self.lose_evidence else [{"id": "e1"}]}
            return {"status": "succeeded", "result": {"evidence": [{"id": "e1"}]}}
    api = API()
    counts = verifier.verify_account_data(api, session, frozen)
    assert counts["history_versions_checked"] == 2
    assert counts["evidence_records_checked"] == 1
    api.lose_history = True
    with pytest.raises(verifier.VerificationError, match="history_preservation_failed"):
        verifier.verify_account_data(api, session, frozen)
    api.lose_history, api.lose_evidence = False, True
    with pytest.raises(verifier.VerificationError, match="evidence_preservation_failed"):
        verifier.verify_account_data(api, session, frozen)
