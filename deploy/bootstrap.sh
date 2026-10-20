#!/usr/bin/env bash
# Run from a reviewed source bundle on the target VPS. Never prints credentials.
set -euo pipefail
umask 027
[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
[[ $(uname -m) == x86_64 ]] || { echo "This bundle targets amd64." >&2; exit 1; }
. /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 24.04 ]] || { echo "Expected Ubuntu 24.04." >&2; exit 1; }
source_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
exec 9>/run/lock/goutou-maintenance.lock
flock -n 9 || { echo "A deployment or backup is already running." >&2; exit 1; }
[[ -f "$source_root/services/gateway/requirements.lock" ]]
if [[ -e /opt/goutou && ! -f /opt/goutou/.managed-bootstrap ]]; then
    echo "Existing unmanaged /opt/goutou: inspect before installation." >&2
    exit 1
fi
for port in 8000 8090; do
    if ss -H -ltn "sport = :$port" | grep -q . && [[ ! -f /opt/goutou/.managed-bootstrap ]]; then
        echo "Port $port is already occupied." >&2
        exit 1
    fi
done
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends ca-certificates curl unzip python3-venv

for account in goutou-pb goutou-gateway; do
    id "$account" >/dev/null 2>&1 || useradd --system --user-group --no-create-home --shell /usr/sbin/nologin "$account"
done
install -d -m 755 /opt/goutou /opt/goutou/releases /var/lib/goutou
touch /opt/goutou/.managed-bootstrap
install -d -o goutou-pb -g goutou-pb -m 700 /var/lib/goutou/pocketbase
install -d -o root -g goutou-gateway -m 750 /etc/goutou
install -d -o root -g root -m 700 /var/backups/goutou

pb_version=0.40.5
pb_zip_sha256=1dbea1b01eb3dfe64953513859d01e759c96650c4e3264fa4eecd54b769382e4
pb_path="/opt/goutou/pocketbase-${pb_version}"
if [[ ! -x "$pb_path/pocketbase" ]]; then
    staging=$(mktemp -d /opt/goutou/pb-download.XXXXXX)
    curl --fail --location --retry 3 --connect-timeout 20 \
        "https://github.com/pocketbase/pocketbase/releases/download/v${pb_version}/pocketbase_${pb_version}_linux_amd64.zip" \
        -o "$staging/pocketbase.zip"
    printf '%s  %s\n' "$pb_zip_sha256" "$staging/pocketbase.zip" | sha256sum --check -
    unzip -q "$staging/pocketbase.zip" pocketbase LICENSE.md -d "$staging/unpacked"
    install -d -m 755 "$pb_path"
    install -m 755 "$staging/unpacked/pocketbase" "$pb_path/pocketbase"
    install -m 644 "$staging/unpacked/LICENSE.md" "$pb_path/LICENSE.md"
fi

if [[ ! -f /etc/goutou/gateway.env ]]; then
    python3 - <<'PY'
import os, secrets
path = "/etc/goutou/gateway.env"
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as file:
    file.write("GOUTOU_OPERATOR_TOKEN=" + secrets.token_urlsafe(48) + "\n")
    file.write("GOUTOU_MODEL_CONFIG=/etc/goutou/models.yaml\n")
    file.write("DEEPSEEK_API_KEY=\n")
PY
fi
# Independent private PB credential; provider Keys are never passed to PB.
python3 - <<'PY'
import os, pathlib, secrets, tempfile
gateway = pathlib.Path('/etc/goutou/gateway.env')
pocketbase = pathlib.Path('/etc/goutou/pocketbase.env')
def configured(path):
    if not path.exists(): return ''
    for line in path.read_text().splitlines():
        if line.startswith('GOUTOU_PB_INTERNAL_KEY='): return line.split('=', 1)[1]
    return ''
left, right = configured(gateway), configured(pocketbase)
if left and right and left != right:
    raise SystemExit('Internal service credentials disagree; inspect privately')
token = left or right or secrets.token_urlsafe(48)
if len(token) < 40: raise SystemExit('Internal service credential is too short')
for path in (gateway, pocketbase):
    if configured(path): continue
    original = path.read_text() if path.exists() else ''
    fd, name = tempfile.mkstemp(prefix='.cloud-env-', dir=path.parent)
    with os.fdopen(fd, 'w') as output:
        os.chmod(name, 0o600)
        output.write(original.rstrip('\n')+'\nGOUTOU_PB_INTERNAL_KEY='+token+'\n')
        output.flush(); os.fsync(output.fileno())
    os.replace(name, path)
PY
if [[ ! -f /etc/goutou/models.yaml ]]; then
    install -o root -g goutou-gateway -m 640 \
        "$source_root/services/gateway/config/models.example.yaml" /etc/goutou/models.yaml
fi
# Never upsert/reset an existing administrator. Refuse ambiguous partial initialization.
if [[ ! -f /etc/goutou/pocketbase-admin.json ]]; then
    if [[ -f /var/lib/goutou/pocketbase/data.db && ! -f /etc/goutou/pocketbase-admin.json.pending ]]; then
        echo "Existing PB data without managed admin record; inspect manually." >&2
        exit 1
    fi
    python3 - <<'PY'
import json, os, secrets, subprocess, tempfile, pathlib, pwd
password = secrets.token_urlsafe(32)
email = "admin@zovoeh.cn"
credential_path = "/etc/goutou/pocketbase-admin.json"
# Persist before initialization so an interrupted run does not lose its password.
pending_path = credential_path + ".pending"
if os.path.exists(pending_path):
    prior = json.loads(pathlib.Path(pending_path).read_text())
    email, password = prior["email"], prior["password"]
else:
    fd = os.open(pending_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump({"email": email, "password": password}, file)
account = pwd.getpwnam("goutou-pb")
with tempfile.TemporaryDirectory(prefix="goutou-init-", dir="/run") as directory:
    os.chown(directory, account.pw_uid, account.pw_gid)
    migration = pathlib.Path(directory) / "1792540800_initial_operator.js"
    migration.write_text(
        'migrate((app) => { if (app.countRecords("_superusers") !== 0) {'
        'throw new Error("Refusing existing administrator"); }'
        'const record = new Record(app.findCollectionByNameOrId("_superusers"));'
        'record.set("email", ' + json.dumps(email) + ');'
        'record.set("password", ' + json.dumps(password) + '); app.save(record); });\n'
    )
    os.chmod(migration, 0o600)
    os.chown(migration, account.pw_uid, account.pw_gid)
    hooks = pathlib.Path(directory) / "empty-hooks"
    hooks.mkdir(mode=0o700)
    os.chown(hooks, account.pw_uid, account.pw_gid)
    result = subprocess.run([
        "runuser", "-u", "goutou-pb", "--",
        "/opt/goutou/pocketbase-0.40.5/pocketbase", "migrate", "up",
        "--dir=/var/lib/goutou/pocketbase", "--migrationsDir=" + directory,
        "--hooksDir=" + str(hooks),
        "--automigrate=false", "--hooksWatch=false"
    ], capture_output=True, text=True)
if result.returncode:
    raise SystemExit("PB initialization failed; credentials retained in root-only pending file")
os.replace(pending_path, credential_path)
PY
fi

source_digest=$(cd "$source_root" && find services/gateway/app services/pocketbase/pb_hooks services/pocketbase/pb_migrations -type f \( -name '*.py' -o -name '*.js' \) -print0 |
    sort -z | xargs -0 sha256sum | sha256sum | cut -c1-16)
lock_digest=$(sha256sum "$source_root/services/gateway/requirements.lock" | cut -c1-12)
release="/opt/goutou/releases/bootstrap-${source_digest}-${lock_digest}"
if [[ ! -f "$release/.complete" ]]; then
    install -d -m 755 "$release/services/gateway"
    cp -R "$source_root/services/gateway/app" "$release/services/gateway/"
    cp "$source_root/services/gateway/requirements.lock" "$release/services/gateway/"
    install -d -m 755 "$release/services/pocketbase"
    cp -R "$source_root/services/pocketbase/pb_hooks" "$source_root/services/pocketbase/pb_migrations" "$release/services/pocketbase/"
    find "$release/services" -type d -exec chmod 755 {} +
    find "$release/services" -type f -exec chmod 644 {} +
    python3 -m venv "$release/venv"
    "$release/venv/bin/python" -m pip install --disable-pip-version-check -r "$release/services/gateway/requirements.lock"
    "$release/venv/bin/python" -m pip check
    chmod -R a+rX "$release/venv"
    touch "$release/.complete"
fi
previous=$(readlink -f /opt/goutou/current || true)
unit_backup=$(mktemp -d /run/goutou-units.XXXXXX)
for unit in goutou-pocketbase.service goutou-gateway.service goutou-backup.service goutou-backup.timer; do
    if [[ -f "/etc/systemd/system/$unit" ]]; then
        cp -p "/etc/systemd/system/$unit" "$unit_backup/$unit"
    else
        touch "$unit_backup/$unit.absent"
    fi
done
rollback_activation() {
    status=$?
    trap - ERR
    systemctl stop goutou-gateway || true
    systemctl stop goutou-pocketbase || true
    if [[ -n "$previous" && "$previous" != "$release" ]]; then
        ln -sfn "$previous" /opt/goutou/current.previous
        mv -Tf /opt/goutou/current.previous /opt/goutou/current
    fi
    for unit in goutou-pocketbase.service goutou-gateway.service goutou-backup.service goutou-backup.timer; do
        if [[ -f "$unit_backup/$unit" ]]; then
            cp -p "$unit_backup/$unit" "/etc/systemd/system/$unit"
        elif [[ -f "$unit_backup/$unit.absent" ]]; then
            rm -f -- "/etc/systemd/system/$unit"
        fi
    done
    systemctl daemon-reload
    if [[ -n "$previous" ]]; then
        systemctl restart goutou-pocketbase || true
        systemctl restart goutou-gateway || true
    fi
    echo "Activation failed; previous service definitions restored. No database downgrade attempted." >&2
    exit "$status"
}
trap rollback_activation ERR
ln -sfn "$release" /opt/goutou/current.next
mv -Tf /opt/goutou/current.next /opt/goutou/current
install -m 644 "$source_root"/deploy/systemd/goutou-*.service /etc/systemd/system/
install -m 644 "$source_root/deploy/systemd/goutou-backup.timer" /etc/systemd/system/
install -m 750 "$source_root/deploy/backup-local.sh" /usr/local/sbin/goutou-backup
install -m 700 "$source_root/deploy/set-model-key.py" /usr/local/sbin/goutou-set-model-key
install -m 700 "$source_root/deploy/provision-account.py" /usr/local/sbin/goutou-provision-account
install -m 700 "$source_root/deploy/reset-sync-epoch.py" /usr/local/sbin/goutou-reset-sync-epoch
systemctl daemon-reload
systemctl enable --now goutou-pocketbase
systemctl restart goutou-pocketbase
systemctl enable goutou-gateway
systemctl restart goutou-gateway

ready=0
for attempt in $(seq 1 30); do
    if curl --silent --fail http://127.0.0.1:8000/health/ready >/dev/null; then
        ready=1
        break
    fi
    sleep 1
done
if [[ $ready != 1 ]]; then
    echo "Health verification failed." >&2
    false
fi
/usr/local/sbin/goutou-provision-account
python3 - "$release" <<'PY'
import datetime, json, pathlib, sys
manifest = {
    "stage": "private-cloud-v1", "pocketbase": "0.40.5",
    "gateway_release": sys.argv[1],
    "deployed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "public_access": False, "domain": "api.zovoeh.cn",
    "sync_implemented": True
}
pathlib.Path("/opt/goutou/deployment.json").write_text(json.dumps(manifest, indent=2) + "\n")
PY
systemctl enable --now goutou-backup.timer
trap - ERR
rm -f -- "$unit_backup"/*
rmdir "$unit_backup"
echo "Private cloud services are healthy. Public domain/HTTPS activation is separate."
