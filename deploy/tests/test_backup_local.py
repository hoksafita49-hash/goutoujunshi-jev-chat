"""Run the backup shell script in a temporary filesystem with fake systemd."""

import os
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "backup-local.sh"
STAMP = "20261021T010203Z"
BASE_MEMBERS = {
    "var/lib/goutou/pocketbase/data.db",
    "etc/goutou/gateway.env",
    "opt/goutou/deployment.json",
}
CADDY_MEMBERS = {"etc/caddy/Caddyfile", "var/lib/caddy/certificates/example.crt"}
SERVICES = ("goutou-pocketbase", "goutou-gateway", "caddy")


def find_bash():
    # Windows' default bash.exe may be a WSL launcher without an installed distro.
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            candidate = Path(git).resolve().parent.parent / "bin" / "bash.exe"
            if candidate.exists():
                return str(candidate)
    return shutil.which("bash")


BASH = find_bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="Bash is required for shell behavior tests")


SYSTEMCTL = r"""#!/usr/bin/env bash
set -eu
printf 'systemctl:%s\n' "$*" >> state/events
case "$1" in
    cat) [[ "$2" == caddy.service && -e state/caddy-installed ]] ;;
    is-active) [[ -e "state/active/${3%.service}" ]] ;;
    stop)
        service="${2%.service}"
        [[ "${FAIL_STOP:-}" != "$service" ]] || exit 41
        rm -f -- "state/active/$service"
        ;;
    start)
        service="${2%.service}"
        [[ "${FAIL_START:-}" != "$service" ]] || exit 42
        touch "state/active/$service"
        ;;
    *) exit 90 ;;
esac
"""

TAR = r"""#!/usr/bin/env bash
set -eu
case "$1" in
    -czf)
        printf 'tar:create\n' >> state/events
        # A snapshot must never read data while any original writer is running.
        for service in goutou-pocketbase goutou-gateway caddy; do
            [[ ! -e "state/active/$service" ]] || exit 43
        done
        if [[ "${FAIL_TAR_CREATE:-0}" == 1 ]]; then
            printf 'incomplete archive' > "$2"
            exit 44
        fi
        ;;
    -tzf)
        printf 'tar:verify\n' >> state/events
        [[ "${FAIL_TAR_VERIFY:-0}" != 1 ]] || exit 45
        ;;
    *) exit 91 ;;
esac
exec "$REAL_TAR" "$@"
"""

INSTALL = r"""#!/usr/bin/env bash
set -eu
[[ $# == 4 && "$1" == -d && "$2" == -m && "$3" == 700 ]] || exit 92
printf 'install:700\n' >> state/events
mkdir -p -- "$4"
"""

CHMOD = r"""#!/usr/bin/env bash
set -eu
[[ $# == 2 && "$1" == 600 && -f "$2" ]] || exit 93
printf 'chmod:600\n' >> state/events
"""

RM = r"""#!/usr/bin/env bash
set -eu
if [[ "${FAIL_REMOVE_PARTIAL:-0}" == 1 && "${*: -1}" == *.partial ]]; then
    exit 46
fi
exec /usr/bin/rm "$@"
"""


@pytest.fixture
def backup(tmp_path):
    root = tmp_path / "root"
    state = tmp_path / "state"
    commands = tmp_path / "commands"
    (root / "run/lock").mkdir(parents=True)
    (state / "active").mkdir(parents=True)
    commands.mkdir()
    for member in BASE_MEMBERS | CADDY_MEMBERS:
        target = root / member
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture:" + member, encoding="utf-8")
    (state / "events").touch()
    for name, source in {
        "systemctl": SYSTEMCTL,
        "tar": TAR,
        "install": INSTALL,
        "chmod": CHMOD,
        "rm": RM,
        "date": "#!/usr/bin/env bash\nprintf '%s\\n' '" + STAMP + "'\n",
        "flock": "#!/usr/bin/env bash\n[[ ${FAIL_LOCK:-0} != 1 ]]\n",
    }.items():
        command = commands / name
        command.write_text(source, encoding="utf-8", newline="\n")
        command.chmod(0o755)
    real_tar = subprocess.run(
        [BASH, "-c", "command -v tar"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True,
    ).stdout.strip()
    # Only privileged access and absolute filesystem locations are translated;
    # the production control flow and real tar archive operations run unchanged.
    source = SCRIPT.read_text(encoding="utf-8")
    source = source.replace("[[ $EUID -eq 0 ]]", "true")
    source = source.replace("/run/lock/goutou-maintenance.lock", "./root/run/lock/goutou-maintenance.lock")
    source = source.replace("/var/backups/goutou", "./root/var/backups/goutou")
    source = source.replace("-C /", "-C ./root")
    script = tmp_path / "backup.sh"
    script.write_text(source, encoding="utf-8", newline="\n")

    class Backup:
        directory = root / "var/backups/goutou"
        archive = directory / ("goutou-" + STAMP + ".tar.gz")

        def run(self, *, installed=False, active=SERVICES[:2], **failures):
            if installed:
                (state / "caddy-installed").touch()
            for service in active:
                (state / "active" / service).touch()
            env = os.environ.copy()
            env.update({"REAL_TAR": real_tar, "LC_ALL": "C", **failures})
            # Set PATH inside Bash so MSYS receives POSIX separators.
            return subprocess.run(
                [BASH, "-c", 'export PATH="$PWD/commands:$PATH"; exec bash backup.sh'],
                cwd=tmp_path, env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=20,
            )

        def events(self):
            return (state / "events").read_text(encoding="utf-8").splitlines()

        def mutations(self):
            return [event for event in self.events() if event.startswith(("systemctl:stop", "systemctl:start", "tar:"))]

        def active(self):
            return {path.name for path in (state / "active").iterdir()}

        def members(self):
            with tarfile.open(self.archive, "r:gz") as archive:
                return {member.name for member in archive.getmembers() if member.isfile()}

    return Backup()


def test_without_caddy_keeps_original_archive_and_service_behavior(backup):
    result = backup.run()
    assert result.returncode == 0, result.stderr
    assert backup.members() == BASE_MEMBERS
    assert backup.active() == {"goutou-pocketbase", "goutou-gateway"}
    assert backup.mutations() == [
        "systemctl:stop goutou-gateway", "systemctl:stop goutou-pocketbase",
        "tar:create", "tar:verify",
        "systemctl:start goutou-pocketbase", "systemctl:start goutou-gateway",
    ]


def test_active_caddy_stops_for_snapshot_and_restarts_after_application(backup):
    result = backup.run(installed=True, active=SERVICES)
    assert result.returncode == 0, result.stderr
    assert backup.members() == BASE_MEMBERS | CADDY_MEMBERS
    assert backup.active() == set(SERVICES)
    assert backup.mutations() == [
        "systemctl:stop caddy", "systemctl:stop goutou-gateway", "systemctl:stop goutou-pocketbase",
        "tar:create", "tar:verify",
        "systemctl:start goutou-pocketbase", "systemctl:start goutou-gateway", "systemctl:start caddy",
    ]


def test_installed_inactive_caddy_is_archived_without_being_activated(backup):
    result = backup.run(installed=True, active=("goutou-pocketbase",))
    assert result.returncode == 0, result.stderr
    assert backup.members() == BASE_MEMBERS | CADDY_MEMBERS
    assert backup.active() == {"goutou-pocketbase"}
    assert "systemctl:stop caddy" not in backup.events()
    assert "systemctl:start caddy" not in backup.events()
    assert "systemctl:start goutou-gateway" not in backup.events()


@pytest.mark.parametrize("failure", ["FAIL_TAR_CREATE", "FAIL_TAR_VERIFY"])
def test_failed_archive_removes_partial_and_restores_all_original_writers(backup, failure):
    result = backup.run(installed=True, active=SERVICES, **{failure: "1"})
    assert result.returncode != 0
    assert backup.active() == set(SERVICES)
    assert backup.events()[-3:] == [
        "systemctl:start goutou-pocketbase", "systemctl:start goutou-gateway", "systemctl:start caddy",
    ]
    assert not backup.archive.exists()
    assert list(backup.directory.glob("*.partial")) == []


def test_failed_partial_cleanup_still_restores_all_original_services(backup):
    result = backup.run(FAIL_TAR_CREATE="1", FAIL_REMOVE_PARTIAL="1")
    assert result.returncode != 0
    assert backup.active() == {"goutou-pocketbase", "goutou-gateway"}
    assert backup.events()[-2:] == [
        "systemctl:start goutou-pocketbase", "systemctl:start goutou-gateway",
    ]


@pytest.mark.parametrize("service", SERVICES)
def test_failed_restart_still_attempts_every_other_original_service(backup, service):
    result = backup.run(installed=True, active=SERVICES, FAIL_START=service)
    assert result.returncode != 0
    assert backup.active() == set(SERVICES) - {service}
    assert backup.events()[-3:] == [
        "systemctl:start goutou-pocketbase", "systemctl:start goutou-gateway", "systemctl:start caddy",
    ]
    assert backup.members() == BASE_MEMBERS | CADDY_MEMBERS


@pytest.mark.parametrize("service", SERVICES)
def test_failed_stop_restores_original_state_without_taking_snapshot(backup, service):
    result = backup.run(installed=True, active=SERVICES, FAIL_STOP=service)
    assert result.returncode != 0
    assert backup.active() == set(SERVICES)
    assert backup.events()[-3:] == [
        "systemctl:start goutou-pocketbase", "systemctl:start goutou-gateway", "systemctl:start caddy",
    ]
    assert "tar:create" not in backup.events()


def test_retention_keeps_seven_completed_archives_and_ignores_other_files(backup):
    backup.directory.mkdir(parents=True)
    for day in range(11, 20):
        (backup.directory / f"goutou-202610{day}T010203Z.tar.gz").touch()
    unrelated = backup.directory / "manual-backup.tar.gz"
    unrelated.touch()
    result = backup.run(installed=True, active=SERVICES)
    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in backup.directory.glob("goutou-*.tar.gz")) == [
        "goutou-20261014T010203Z.tar.gz", "goutou-20261015T010203Z.tar.gz",
        "goutou-20261016T010203Z.tar.gz", "goutou-20261017T010203Z.tar.gz",
        "goutou-20261018T010203Z.tar.gz", "goutou-20261019T010203Z.tar.gz",
        "goutou-20261021T010203Z.tar.gz",
    ]
    assert unrelated.exists()
    assert "install:700" in backup.events()
    assert "chmod:600" in backup.events()
    if os.name != "nt":
        assert backup.directory.stat().st_mode & 0o777 == 0o700
        assert backup.archive.stat().st_mode & 0o777 == 0o600


def test_held_maintenance_lock_prevents_service_mutation(backup):
    result = backup.run(installed=True, active=SERVICES, FAIL_LOCK="1")
    assert result.returncode != 0
    assert backup.events() == []
    assert backup.active() == set(SERVICES)
    assert not backup.directory.exists()
