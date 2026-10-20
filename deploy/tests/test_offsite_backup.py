"""Exercise local capture/state with real archives; replace only network and flock."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import gzip
import importlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile

import pytest


OFFSITE = Path(__file__).resolve().parents[1] / "offsite"
sys.path.insert(0, str(OFFSITE))


def module(name):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        pytest.fail(f"Offsite implementation is missing: {exc.name}")


CONFIG = {
    "version": 1, "bucket": "zovoeh-backup-20261021",
    "endpoint": "https://oss-cn-beijing.aliyuncs.com", "prefix": "goutou-v1",
    "access_key_id": "fixture-access-key", "access_key_secret": "fixture-secret-key",
    "password": "fixture-encryption-password-with-32-characters",
}
NOW = datetime(2026, 10, 21, 5, tzinfo=timezone.utc)
SNAPSHOT = "a" * 64


def test_provider_environment_keeps_credentials_out_of_arguments_and_errors(tmp_path):
    common = module("offsite_common")
    env = common.tool_env(CONFIG)
    assert env["RESTIC_REPOSITORY"] == "rclone:goutouoss:zovoeh-backup-20261021/goutou-v1"
    assert env["RESTIC_PASSWORD"] == CONFIG["password"]
    assert env["RCLONE_CONFIG"] == os.devnull
    assert env["RCLONE_CONFIG_GOUTOUOSS_PROVIDER"] == "Alibaba"
    assert env["RCLONE_CONFIG_GOUTOUOSS_ACL"] == "private"
    assert env["RCLONE_CONFIG_GOUTOUOSS_NO_CHECK_BUCKET"] == "true"
    code = "import os,sys; print(os.environ['RESTIC_PASSWORD'], file=sys.stderr); sys.exit(9)"
    with pytest.raises(common.OffsiteError) as caught:
        common.run_tool([sys.executable, "-c", code], CONFIG, timeout=3)
    assert str(caught.value) == "tool_failed"
    assert CONFIG["password"] not in repr(caught.value)
    result = common.run_tool([sys.executable, "-c", "print('safe'); import sys; print('hidden',file=sys.stderr)"], CONFIG, timeout=3)
    assert result.stdout.strip() == "safe"
    assert result.stderr == ""


def test_tool_timeout_is_bounded_and_safe():
    common = module("offsite_common")
    with pytest.raises(common.OffsiteError, match="^tool_timeout$"):
        common.run_tool([sys.executable, "-c", "import time; time.sleep(5)"], CONFIG, timeout=0.05)


def test_config_accepts_authorized_16_character_password():
    common = module("offsite_common")
    assert common.validate_config({**CONFIG, "password": "a" * 16})["password"] == "a" * 16


def test_config_rejects_password_below_authorized_minimum():
    common = module("offsite_common")
    with pytest.raises(common.OffsiteError) as caught:
        common.validate_config({**CONFIG, "password": "a" * 15})
    assert caught.value.code == "config_invalid"


def test_config_rejects_nonprivate_files_and_invalid_repositories(tmp_path):
    common = module("offsite_common")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(CONFIG), encoding="utf-8")
    assert common.load_config(path, file_check=lambda p: None) == CONFIG
    insecure = os.stat_result((stat.S_IFREG | 0o644, 0, 0, 1, 0, 0, 1, 0, 0, 0))
    with pytest.raises(common.OffsiteError, match="^file_not_private$"):
        common.ensure_private_regular(path, stat_fn=lambda p: insecure)
    nonroot = os.stat_result((stat.S_IFREG | 0o600, 0, 0, 1, 1000, 0, 1, 0, 0, 0))
    with pytest.raises(common.OffsiteError, match="^file_not_private$"):
        common.ensure_private_regular(path, stat_fn=lambda p: nonroot)
    link = os.stat_result((stat.S_IFLNK | 0o600, 0, 0, 1, 0, 0, 1, 0, 0, 0))
    with pytest.raises(common.OffsiteError, match="^file_not_private$"):
        common.ensure_private_regular(path, stat_fn=lambda p: link)
    for changed in ({"prefix": "../escape"}, {"endpoint": "https://attacker.invalid"}, {"version": 2}):
        with pytest.raises(common.OffsiteError, match="^config_invalid$"):
            common.validate_config({**CONFIG, **changed})


@pytest.fixture
def fixture(tmp_path):
    common = module("offsite_common")
    backup = module("offsite_backup")
    paths = backup.Paths(
        backup_dir=tmp_path / "backups", releases_dir=tmp_path / "releases",
        pocketbase_dir=tmp_path / "pocketbase-0.40.5", wheels_dir=tmp_path / "offsite-runtime/wheels",
        caddy_binary=tmp_path / "usr/bin/caddy", systemd_dirs=(tmp_path / "units",),
        work_dir=tmp_path / "offsite-work", state_file=tmp_path / "offsite-state.json",
        maintenance_lock=tmp_path / "maintenance.lock", offsite_lock=tmp_path / "offsite.lock",
    )
    release = paths.releases_dir / "bootstrap-fixture"
    contents = {
        release / ".complete": b"",
        release / "services/gateway/app/main.py": b"application = 'fixture'\n",
        release / "services/gateway/requirements.lock": b"example==1.0\n",
        release / "services/pocketbase/pb_hooks/main.pb.js": b"// hooks\n",
        release / "services/pocketbase/pb_hooks/cloud.js": b"// cloud\n",
        release / "services/pocketbase/pb_hooks/evidence_memory.js": b"// evidence\n",
        release / "services/pocketbase/pb_migrations/001.js": b"// migration\n",
        release / "services/gateway/app/__pycache__/ignored.pyc": b"cache",
        release / "venv/bin/python": b"must not be copied",
        paths.pocketbase_dir / "pocketbase": b"pocketbase executable",
        paths.pocketbase_dir / "LICENSE.md": b"MIT fixture",
        paths.caddy_binary: b"caddy executable",
        paths.wheels_dir / "example-1.0-py3-none-any.whl": b"wheel fixture",
        paths.systemd_dirs[0] / "goutou-gateway.service": b"[Service]\n",
        paths.systemd_dirs[0] / "caddy.service": b"[Service]\n",
    }
    for path, content in contents.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    lock_digest = hashlib.sha256(b"example==1.0\n").hexdigest()
    (paths.wheels_dir.parent / "requirements.lock.sha256").write_text(lock_digest + "\n", encoding="utf-8")
    wheel_digest = hashlib.sha256(b"wheel fixture").hexdigest()
    (paths.wheels_dir.parent / "WHEELS.json").write_text(json.dumps({"example-1.0-py3-none-any.whl": wheel_digest}), encoding="utf-8")

    class Fixture:
        def __init__(self):
            self.paths = paths
            self.release = release
            self.now = NOW
            self.calls = []
            self.events = []
            self.held = set()
            self.fail_upload = False
            self.fail_retention = False
            self.manifests = []
            self.staged = []

        def archive(self, *, name="goutou-20261021T042000Z.tar.gz", release_path=None, extra=None):
            paths.backup_dir.mkdir(parents=True, exist_ok=True)
            target = paths.backup_dir / name
            manifest = {"gateway_release": str(release_path or release), "pocketbase": "0.40.5"}
            entries = {
                "opt/goutou/deployment.json": json.dumps(manifest).encode(),
                "var/lib/goutou/pocketbase/data.db": b"cold database fixture",
                "etc/goutou/gateway.env": b"MODEL_KEY=application-secret\n",
                **(extra or {}),
            }
            with tarfile.open(target, "w:gz") as archive:
                for name, data in entries.items():
                    member = tarfile.TarInfo(name)
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
            target.chmod(0o600)
            return target

        @contextmanager
        def lock(self, path):
            assert path not in self.held
            self.held.add(path)
            self.events.append(("lock", path.name))
            try:
                yield
            finally:
                self.events.append(("unlock", path.name))
                self.held.remove(path)

        def file_check(self, path):
            # POSIX mode/uid checks are covered separately. Windows has no chmod ACL equivalent.
            if path.is_symlink() or not path.is_file():
                raise common.OffsiteError("file_not_private")

        def run_tool(self, args, config, timeout):
            assert paths.maintenance_lock not in self.held
            assert paths.offsite_lock in self.held
            self.calls.append((args, timeout))
            if args[1] == "backup":
                bundle = Path(args[2])
                assert bundle.is_absolute() and bundle.name == "bundle"
                self.staged.append(bundle)
                self.manifests.append(json.loads((bundle / "bundle-manifest.json").read_text()))
                for relative, digest in self.manifests[-1]["files"].items():
                    assert hashlib.sha256((bundle / relative).read_bytes()).hexdigest() == digest
                if self.fail_upload:
                    raise common.OffsiteError("tool_failed")
                return subprocess.CompletedProcess(args, 0, json.dumps({"message_type": "summary", "snapshot_id": SNAPSHOT}) + "\n", "")
            assert args[1] == "forget", args
            if self.fail_retention:
                raise common.OffsiteError("tool_failed")
            return subprocess.CompletedProcess(args, 0, "", "")

        def engine(self, **kwargs):
            return backup.BackupEngine(
                CONFIG, paths=paths, runner=self.run_tool, lock_factory=self.lock,
                now=lambda: self.now, root_check=lambda: None, file_check=self.file_check,
                **kwargs,
            )

    fixture = Fixture()
    fixture.archive()
    return fixture


def test_success_captures_exact_release_hashes_and_releases_maintenance_before_network(fixture):
    result = fixture.engine().run()
    assert result["status"] == "uploaded"
    state = json.loads(fixture.paths.state_file.read_text())
    assert state["snapshot_id"] == SNAPSHOT
    assert state["archive_name"] == "goutou-20261021T042000Z.tar.gz"
    assert state["time"] == "2026-10-21T05:00:00Z"
    assert len(state["digest"]) == 64
    manifest = fixture.manifests[0]
    assert manifest["schema"] == "goutou-offsite-bundle-v1"
    assert manifest["source_release"] == "bootstrap-fixture"
    assert manifest["pocketbase_version"] == "0.40.5"
    assert manifest["python_version"] == "3.12"
    assert manifest["files"]["runtime/services/gateway/app/main.py"] == hashlib.sha256(b"application = 'fixture'\n").hexdigest()
    assert "runtime/pocketbase/LICENSE.md" in manifest["files"]
    assert "runtime/WHEELS.json" in manifest["files"]
    assert not any("venv" in name or "__pycache__" in name for name in manifest["files"])
    assert not fixture.staged[0].exists()
    assert list(fixture.paths.work_dir.iterdir()) == []
    args, timeout = fixture.calls[0]
    assert args[3:] == ["--tag", "goutou-production-v1", "--host", "goutou-private-vps", "--json", "--limit-upload", "256"]
    assert 1024 < timeout <= 1800
    assert fixture.calls[1][0] == [
        "restic", "forget", "--tag", "goutou-production-v1", "--host", "goutou-private-vps",
        "--group-by", "host,tags", "--keep-daily", "7", "--keep-weekly", "4", "--keep-monthly", "3", "--prune",
        "--limit-upload", "256", "--max-repack-size", "64M",
    ]
    assert all(secret not in repr(fixture.calls) for secret in (CONFIG["password"], CONFIG["access_key_secret"]))


def test_retention_limits_repack_upload_bandwidth_and_size(fixture):
    fixture.engine().run()
    args, timeout = next(call for call in fixture.calls if call[0][1] == "forget")
    assert "--limit-upload" in args
    assert args[args.index("--limit-upload") + 1] == "256"
    assert "--max-repack-size" in args
    assert args[args.index("--max-repack-size") + 1] == "64M"
    assert timeout == 900


@pytest.mark.parametrize("extra", [{"../escape": b"bad"}, {"/absolute": b"bad"}, {"etc/goutou-offsite/config.json": b"bad"}])
def test_unsafe_archive_member_is_rejected_before_upload(fixture, extra):
    fixture.archive(extra=extra)
    common = module("offsite_common")
    with pytest.raises(common.OffsiteError, match="^archive_unsafe$"):
        fixture.engine().run()
    assert fixture.calls == []
    assert not fixture.paths.state_file.exists()


@pytest.mark.parametrize("age_hours", [31, -1])
def test_old_or_future_archive_is_rejected(fixture, age_hours):
    for archive in fixture.paths.backup_dir.iterdir():
        archive.unlink()
    stamp = (NOW - timedelta(hours=age_hours)).strftime("%Y%m%dT%H%M%SZ")
    fixture.archive(name=f"goutou-{stamp}.tar.gz")
    with pytest.raises(module("offsite_common").OffsiteError, match="^archive_stale$"):
        fixture.engine().run()
    assert fixture.calls == []


def test_corrupt_latest_archive_fails_without_falling_back(fixture):
    fixture.archive(name="goutou-20261021T041000Z.tar.gz")
    target = fixture.paths.backup_dir / "goutou-20261021T042000Z.tar.gz"
    target.write_bytes(b"not gzip")
    with pytest.raises(module("offsite_common").OffsiteError, match="^archive_invalid$"):
        fixture.engine().run()
    assert fixture.calls == []


def test_gzip_crc_is_checked_after_tar_end_marker(fixture):
    target = fixture.paths.backup_dir / "goutou-20261021T042000Z.tar.gz"
    payload = bytearray(target.read_bytes())
    payload[-8] ^= 0x01
    target.write_bytes(payload)
    with pytest.raises(module("offsite_common").OffsiteError, match="^archive_invalid$"):
        fixture.engine().run()
    assert fixture.calls == []


def test_decompressed_data_after_tar_end_is_bounded(fixture, monkeypatch):
    backup = module("offsite_backup")
    target = fixture.paths.backup_dir / "goutou-20261021T042000Z.tar.gz"
    original = gzip.decompress(target.read_bytes())
    monkeypatch.setattr(backup, "MAX_ARCHIVE_EXPANDED", len(original) + 1024)
    target.write_bytes(gzip.compress(original + b"x" * 2048))
    with pytest.raises(module("offsite_common").OffsiteError, match="^archive_too_large$"):
        fixture.engine().run()
    assert fixture.calls == []


def test_tar_symlink_is_not_a_safe_completed_archive(fixture):
    target = fixture.paths.backup_dir / "goutou-20261021T042000Z.tar.gz"
    with tarfile.open(target, "w:gz") as archive:
        member = tarfile.TarInfo("etc/goutou/secret")
        member.type = tarfile.SYMTYPE
        member.linkname = "../../outside"
        archive.addfile(member)
    with pytest.raises(module("offsite_common").OffsiteError, match="^archive_unsafe$"):
        fixture.engine().run()
    assert fixture.calls == []


def test_archive_release_traversal_is_rejected(fixture):
    fixture.archive(release_path=str(fixture.paths.releases_dir / ".." / "other"))
    with pytest.raises(module("offsite_common").OffsiteError, match="^release_invalid$"):
        fixture.engine().run()
    assert fixture.calls == []


@pytest.mark.parametrize("missing", ["pocketbase", "license", "services", "wheels", "hooks_main", "hooks_cloud", "hooks_evidence"])
def test_missing_mandatory_runtime_fails_before_upload(fixture, missing):
    targets = {
        "pocketbase": fixture.paths.pocketbase_dir / "pocketbase",
        "license": fixture.paths.pocketbase_dir / "LICENSE.md",
        "services": fixture.release / "services/gateway/app/main.py",
        "wheels": fixture.paths.wheels_dir / "example-1.0-py3-none-any.whl",
        "hooks_main": fixture.release / "services/pocketbase/pb_hooks/main.pb.js",
        "hooks_cloud": fixture.release / "services/pocketbase/pb_hooks/cloud.js",
        "hooks_evidence": fixture.release / "services/pocketbase/pb_hooks/evidence_memory.js",
    }
    targets[missing].unlink()
    with pytest.raises(module("offsite_common").OffsiteError, match="^runtime_missing$"):
        fixture.engine().run()
    assert fixture.calls == []


@pytest.mark.parametrize("changed", ["lock", "wheel"])
def test_mismatched_offline_wheels_fail_before_upload(fixture, changed):
    target = fixture.release / "services/gateway/requirements.lock" if changed == "lock" else fixture.paths.wheels_dir / "example-1.0-py3-none-any.whl"
    target.write_bytes(b"unexpected release or corrupt wheel")
    with pytest.raises(module("offsite_common").OffsiteError, match="^runtime_mismatch$"):
        fixture.engine().run()
    assert fixture.calls == []


def test_stage_size_limit_prevents_network_and_cleans_only_owned_capture(fixture):
    fixture.paths.work_dir.mkdir()
    unrelated = fixture.paths.work_dir / "keep-me"
    unrelated.write_text("unrelated", encoding="utf-8")
    with pytest.raises(module("offsite_common").OffsiteError, match="^stage_too_large$"):
        fixture.engine(max_bytes=30).run()
    assert fixture.calls == []
    assert unrelated.read_text() == "unrelated"
    assert list(fixture.paths.work_dir.iterdir()) == [unrelated]


def test_aggregate_runtime_limit_is_enforced_during_capture(fixture):
    archive = fixture.paths.backup_dir / "goutou-20261021T042000Z.tar.gz"
    with pytest.raises(module("offsite_common").OffsiteError, match="^stage_too_large$"):
        fixture.engine(max_bytes=archive.stat().st_size + 10).run()
    assert fixture.calls == []
    assert list(fixture.paths.work_dir.iterdir()) == []


def test_upload_failure_never_records_success_or_prunes_and_cleans_capture(fixture):
    fixture.fail_upload = True
    with pytest.raises(module("offsite_common").OffsiteError, match="^tool_failed$"):
        fixture.engine().run()
    assert not fixture.paths.state_file.exists()
    assert len(fixture.calls) == 1
    assert not fixture.staged[0].exists()


def test_retention_failure_preserves_success_and_reports_degraded(fixture):
    fixture.fail_retention = True
    result = fixture.engine().run()
    assert result == {"status": "degraded", "code": "retention_failed", "snapshot_id": SNAPSHOT}
    assert json.loads(fixture.paths.state_file.read_text())["snapshot_id"] == SNAPSHOT
    assert not fixture.staged[0].exists()


def test_duplicate_is_skipped_even_when_forced(fixture):
    fixture.engine().run()
    fixture.now += timedelta(days=1)
    # Keep the source archive within the permitted 30h age window.
    for force in (False, True):
        assert fixture.engine().run(force=force) == {"status": "skipped", "code": "already_uploaded"}
    assert len(fixture.calls) == 2


def test_successful_daily_limit_and_manual_force(fixture):
    fixture.engine().run()
    fixture.archive(name="goutou-20261021T045000Z.tar.gz", extra={"etc/goutou/extra": b"new"})
    assert fixture.engine().run() == {"status": "skipped", "code": "daily_limit"}
    assert fixture.engine().run(force=True)["status"] == "uploaded"
    assert len(fixture.calls) == 4


def test_daily_ledger_records_upload_completion_date(fixture):
    original_runner = fixture.run_tool

    def finish_next_day(args, config, timeout):
        result = original_runner(args, config, timeout)
        if args[1] == "backup":
            fixture.now = NOW + timedelta(days=1)
        return result

    engine = fixture.engine()
    engine.runner = finish_next_day
    engine.run()
    state = json.loads(fixture.paths.state_file.read_text())
    assert state["time"] == "2026-10-22T05:00:00Z"
    assert state["successful_dates"] == ["2026-10-22"]
    fixture.archive(name="goutou-20261022T043000Z.tar.gz")
    assert fixture.engine().run() == {"status": "skipped", "code": "daily_limit"}
    assert len(fixture.calls) == 2


def test_previous_success_cannot_be_reuploaded_after_a_newer_success(fixture):
    fixture.engine().run()
    newer = fixture.archive(name="goutou-20261021T045000Z.tar.gz", extra={"etc/goutou/extra": b"new"})
    fixture.engine().run(force=True)
    newer.unlink()
    assert fixture.engine().run(force=True) == {"status": "skipped", "code": "already_uploaded"}
    assert len(fixture.calls) == 4


def test_missing_snapshot_id_never_records_success_or_prunes(fixture):
    original_runner = fixture.run_tool

    def malformed(args, config, timeout):
        result = original_runner(args, config, timeout)
        return subprocess.CompletedProcess(args, 0, '{"message_type":"summary"}\n', "")

    engine = fixture.engine()
    engine.runner = malformed
    with pytest.raises(module("offsite_common").OffsiteError, match="^backup_result_invalid$"):
        engine.run()
    assert not fixture.paths.state_file.exists()
    assert len(fixture.calls) == 1


def test_readonly_check_does_not_create_directories_or_run_tools(fixture):
    result = fixture.engine().check()
    assert result == {"status": "ready", "configured": True, "latest_success": None}
    assert fixture.calls == []
    assert not fixture.paths.work_dir.exists()
    assert not fixture.paths.state_file.exists()


def test_busy_lock_prevents_capture_and_network(fixture):
    common = module("offsite_common")

    @contextmanager
    def busy(path):
        raise common.OffsiteError("busy")
        yield

    engine = fixture.engine()
    engine.lock_factory = busy
    with pytest.raises(common.OffsiteError, match="^busy$"):
        engine.run()
    assert fixture.calls == []
    assert not fixture.paths.work_dir.exists()


def test_busy_maintenance_lock_prevents_capture_and_network(fixture):
    common = module("offsite_common")

    @contextmanager
    def locked(path):
        if path == fixture.paths.maintenance_lock:
            raise common.OffsiteError("busy")
        with fixture.lock(path):
            yield

    engine = fixture.engine()
    engine.lock_factory = locked
    with pytest.raises(common.OffsiteError, match="^busy$"):
        engine.run()
    assert fixture.calls == []
    assert not fixture.paths.work_dir.exists()


def test_check_without_config_is_readonly_and_does_not_probe_cloud(monkeypatch, tmp_path, capsys):
    backup = module("offsite_backup")
    config = tmp_path / "absent/config.json"
    monkeypatch.setattr(backup, "CONFIG_PATH", config)
    monkeypatch.setattr(backup, "require_root", lambda: None)
    assert backup.main(["--check"]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "not_configured", "configured": False, "latest_success": None}
    assert not config.parent.exists()
