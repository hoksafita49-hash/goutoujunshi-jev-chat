"""Exercise HTTPS activation rollback without services, network, or host writes."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "enable-https.sh"


def find_bash():
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
    is-active) [[ -f state/active ]] ;;
    is-enabled)
        cat state/enabled
        [[ $(cat state/enabled) == enabled ]]
        ;;
    enable) printf 'enabled\n' > state/enabled ;;
    disable) printf 'disabled\n' > state/enabled ;;
    start|restart) touch state/active ;;
    stop) rm -f -- state/active ;;
    *) exit 90 ;;
esac
"""

UFW = r"""#!/usr/bin/env bash
set -eu
printf 'ufw:%s\n' "$*" >> state/events
case "$1" in
    status)
        output='Status: active'
        while IFS= read -r rule; do output+=$'\n'"$rule/tcp ALLOW Anywhere"; done < state/firewall
        # Emit small status output in one write, like buffered ufw output;
        # separate writes would add a synthetic SIGPIPE race with grep -q.
        printf '%s\n' "$output"
        ;;
    allow)
        [[ "$2" == 80/tcp || "$2" == 443/tcp ]] || exit 91
        printf '%s\n' "${2%/tcp}" >> state/firewall
        ;;
    --force)
        [[ "$2" == delete && "$3" == allow ]] || exit 92
        [[ "$4" == 80/tcp || "$4" == 443/tcp ]] || exit 93
        grep -vx "${4%/tcp}" state/firewall > state/firewall.next
        mv state/firewall.next state/firewall
        ;;
    *) exit 94 ;;
esac
"""

CURL = r"""#!/usr/bin/env bash
set -eu
case "${*: -1}" in
    http://127.0.0.1:8000/health/ready) exit 0 ;;
    https://api.zovoeh.cn/health/live)
        printf 'curl:tls\n' >> state/events
        [[ "${FAIL_CERTIFICATE:-0}" != 1 ]]
        ;;
    *) exit 95 ;;
esac
"""

MANIFEST = r"""#!/usr/bin/env bash
set -eu
# Consume the real heredoc. Inject a failure after a marker has appeared to
# exercise EXIT cleanup independently of Python platform-specific permissions.
cat >/dev/null
printf '{"fixture":true}\n' > root/etc/goutou/https.json
printf 'manifest:written\n' >> state/events
exit 55
"""


@pytest.fixture
def activation(tmp_path):
    root = tmp_path / "root"
    state = tmp_path / "state"
    commands = tmp_path / "commands"
    for relative in ("opt/goutou", "run/lock", "var/backups/goutou", "etc/caddy", "etc/goutou"):
        (root / relative).mkdir(parents=True)
    state.mkdir()
    commands.mkdir()
    (root / "opt/goutou/.managed-bootstrap").touch()
    (root / "etc/caddy/Caddyfile").write_text("previous private configuration\n", encoding="utf-8")
    (tmp_path / "Caddyfile").write_text("new HTTPS configuration\n", encoding="utf-8")
    (state / "events").touch()
    (state / "firewall").write_text("22\n", encoding="utf-8")
    for name, source in {
        "systemctl": SYSTEMCTL,
        "ufw": UFW,
        "curl": CURL,
        "python3": MANIFEST,
        "getent": "#!/usr/bin/env bash\nprintf '103.146.124.175 STREAM api.zovoeh.cn\\n'\n",
        "caddy": "#!/usr/bin/env bash\n[[ $1 == validate ]]\n",
        "flock": "#!/usr/bin/env bash\nexit 0\n",
        "sleep": "#!/usr/bin/env bash\nexit 0\n",
        "goutou-backup": "#!/usr/bin/env bash\nprintf 'backup\\n' >> state/events\n",
        "runuser": "#!/usr/bin/env bash\n[[ $1 == -u && $2 == caddy && $3 == -- ]] || exit 96\nshift 3\nexec \"$@\"\n",
        "install": "#!/usr/bin/env bash\n[[ $# == 8 && $1 == -o && $2 == root && $3 == -g && $4 == root && $5 == -m && $6 == 644 ]] || exit 97\ncp -- \"$7\" \"$8\"\n",
    }.items():
        target = commands / name
        target.write_text(source, encoding="utf-8", newline="\n")
        target.chmod(0o755)
    # Translate only privileged access and absolute paths. Bash executes the
    # production branches, EXIT trap, and polling loop unchanged.
    source = SCRIPT.read_text(encoding="utf-8")
    for original, mapped in {
        "[[ $EUID -eq 0 ]]": "true",
        "/opt/goutou/.managed-bootstrap": "./root/opt/goutou/.managed-bootstrap",
        "/usr/local/sbin/goutou-backup": "./commands/goutou-backup",
        "/run/lock/goutou-maintenance.lock": "./root/run/lock/goutou-maintenance.lock",
        "/var/backups/goutou": "./root/var/backups/goutou",
        "/etc/caddy/Caddyfile": "./root/etc/caddy/Caddyfile",
        "/etc/goutou": "./root/etc/goutou",
    }.items():
        assert original in source
        source = source.replace(original, mapped)
    (tmp_path / "activation.sh").write_text(source, encoding="utf-8", newline="\n")

    class Activation:
        config = root / "etc/caddy/Caddyfile"
        marker = root / "etc/goutou/https.json"

        def run(self, *, active=False, enabled=False, certificate_fails=True):
            if active:
                (state / "active").touch()
            (state / "enabled").write_text("enabled\n" if enabled else "disabled\n", encoding="utf-8")
            env = os.environ.copy()
            env.update(LC_ALL="C", FAIL_CERTIFICATE="1" if certificate_fails else "0")
            return subprocess.run(
                [BASH, "-c", 'export PATH="$PWD/commands:$PATH"; exec bash activation.sh'],
                cwd=tmp_path, env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
            )

        def events(self):
            return (state / "events").read_text(encoding="utf-8").splitlines()

        def assert_restored(self, *, active, enabled):
            assert self.config.read_text(encoding="utf-8") == "previous private configuration\n"
            assert (state / "firewall").read_text(encoding="utf-8").splitlines() == ["22"]
            assert (state / "active").exists() is active
            assert (state / "enabled").read_text(encoding="utf-8").strip() == ("enabled" if enabled else "disabled")
            assert not self.marker.exists()
            firewall_changes = [event for event in self.events() if event.startswith("ufw:") and event != "ufw:status"]
            assert firewall_changes == [
                "ufw:allow 80/tcp comment goutou-https",
                "ufw:allow 443/tcp comment goutou-https",
                "ufw:--force delete allow 443/tcp",
                "ufw:--force delete allow 80/tcp",
            ]

    return Activation()


@pytest.mark.parametrize("active,enabled", [(False, False), (True, True)])
def test_certificate_timeout_restores_config_firewall_and_original_unit_state(activation, active, enabled):
    result = activation.run(active=active, enabled=enabled)
    assert result.returncode != 0, result.stdout
    assert "Trusted TLS certificate was not ready in time" in result.stderr
    assert activation.events().count("curl:tls") == 120
    activation.assert_restored(active=active, enabled=enabled)
    if enabled:
        assert "systemctl:enable caddy" not in activation.events()
        assert "systemctl:disable caddy" not in activation.events()
    else:
        assert "systemctl:enable caddy" in activation.events()
        assert "systemctl:disable caddy" in activation.events()


def test_failure_after_manifest_creation_removes_marker_and_rolls_back(activation):
    result = activation.run(certificate_fails=False)
    assert result.returncode == 55, result.stderr
    assert "manifest:written" in activation.events()
    activation.assert_restored(active=False, enabled=False)
