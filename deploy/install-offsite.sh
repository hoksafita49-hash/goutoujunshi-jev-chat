#!/usr/bin/env bash
# Install tools and isolated recovery materials; never activates cloud uploads.
set -euo pipefail
umask 077
[[ $EUID -eq 0 ]] || { echo 'Run as root.' >&2; exit 1; }
[[ -f /opt/goutou/.managed-bootstrap ]]
source_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
for name in offsite_common.py offsite_backup.py configure_offsite.py verify_restore.py; do
    [[ -f "$source_root/deploy/offsite/$name" ]]
done
exec 9>/run/lock/goutou-offsite.lock
flock -n 9 || { echo 'Offsite setup or backup is already running.' >&2; exit 1; }

if ! command -v restic >/dev/null || ! command -v rclone >/dev/null; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y --no-install-recommends restic rclone
fi
python3 - <<'PY'
import sys
if sys.version_info[:2] != (3, 12):
    raise SystemExit('Expected Python3.12 for the pinned recovery wheelhouse')
PY
release=$(readlink -f /opt/goutou/current)
[[ $release == /opt/goutou/releases/* && -f "$release/.complete" ]]
[[ -f "$release/services/gateway/requirements.lock" ]]
install -d -m 700 /etc/goutou-offsite /var/lib/goutou/offsite-work

# Preserve a pinned offline wheelhouse so recovery does not require the old VPS
# release or a functioning PyPI mirror. Never build arbitrary source packages.
runtime=/var/lib/goutou/offsite-runtime
lock_hash=$(sha256sum "$release/services/gateway/requirements.lock" | cut -d' ' -f1)
wheelhouse_ok=0
if [[ -f "$runtime/requirements.lock.sha256" && -f "$runtime/WHEELS.json" ]]; then
    if python3 - "$runtime" "$lock_hash" <<'PY'
import hashlib,json,pathlib,sys
p=pathlib.Path(sys.argv[1])
expected=json.loads((p/'WHEELS.json').read_text())
actual={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in (p/'wheels').glob('*.whl')}
assert expected and actual==expected
assert (p/'requirements.lock.sha256').read_text().strip()==sys.argv[2]
PY
    then wheelhouse_ok=1; fi
fi
if [[ $wheelhouse_ok != 1 ]]; then
    staging=$(mktemp -d /var/lib/goutou/.offsite-runtime.XXXXXX)
    cleanup() {
        status=$?
        trap - EXIT
        if [[ -n ${staging:-} && $staging == /var/lib/goutou/.offsite-runtime.* ]]; then
            rm -rf -- "$staging" || status=1
        fi
        exit "$status"
    }
    trap cleanup EXIT
    install -d -m 700 "$staging/wheels"
    "$release/venv/bin/python" -m pip download --disable-pip-version-check \
        --only-binary=:all: --no-deps --dest "$staging/wheels" \
        -r "$release/services/gateway/requirements.lock"
    python3 - "$staging" "$lock_hash" <<'PY'
import hashlib,json,pathlib,sys
p=pathlib.Path(sys.argv[1])
files={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in (p/'wheels').glob('*.whl')}
assert files, 'No offline wheels downloaded'
(p/'WHEELS.json').write_text(json.dumps(files,sort_keys=True,indent=2)+'\n')
(p/'requirements.lock.sha256').write_text(sys.argv[2]+'\n')
PY
    if [[ -e "$runtime" ]]; then
        [[ -d "$runtime" && ! -L "$runtime" ]]
        mv -- "$runtime" "${runtime}.previous.$(date -u +%Y%m%dT%H%M%SZ)"
    fi
    mv -- "$staging" "$runtime"
    staging=''
    trap - EXIT
fi

digest=$(cd "$source_root/deploy/offsite" && sha256sum ./*.py | sha256sum | cut -c1-16)
code="/opt/goutou/offsite-releases/$digest"
install -d -m 755 /opt/goutou/offsite-releases "$code"
if [[ ! -f "$code/.complete" ]]; then
    install -m 644 "$source_root"/deploy/offsite/*.py "$code/"
    python3 -m py_compile "$code"/*.py
    touch "$code/.complete"
fi
ln -sfn "$code" /opt/goutou/offsite-current.next
mv -Tf /opt/goutou/offsite-current.next /opt/goutou/offsite-current
for pair in 'goutou-offsite-backup offsite_backup.py' \
            'goutou-configure-offsite configure_offsite.py' \
            'goutou-verify-offsite-restore verify_restore.py'; do
    read -r command module <<< "$pair"
    cat > "/usr/local/sbin/$command" <<EOF
#!/bin/sh
set -eu
code=\$(readlink -f /opt/goutou/offsite-current)
exec /usr/bin/python3 "\$code/$module" "\$@"
EOF
    chmod 750 "/usr/local/sbin/$command"
done
install -m 644 "$source_root/deploy/systemd/goutou-offsite-backup.service" /etc/systemd/system/
install -m 644 "$source_root/deploy/systemd/goutou-offsite-backup.timer" /etc/systemd/system/
systemctl daemon-reload
echo 'Offsite tools installed. Timer remains unenabled until credentials and a real restore pass.'
