#!/usr/bin/env bash
# Initial public client endpoint activation; install Caddy from its signed package first.
set -euo pipefail
umask 077
[[ $EUID -eq 0 ]] || { echo 'Run as root' >&2; exit 1; }
[[ -f /opt/goutou/.managed-bootstrap ]]
candidate=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/Caddyfile
[[ -f "$candidate" ]]
command -v caddy >/dev/null
[[ $(getent ahostsv4 api.zovoeh.cn | awk '{print $1}' | sort -u) == 103.146.124.175 ]] || {
    echo 'Expected IPv4 DNS record is not ready' >&2; exit 1;
}
curl --silent --fail http://127.0.0.1:8000/health/ready >/dev/null
caddy validate --config "$candidate" --adapter caddyfile
if [[ -f /etc/goutou/https.json ]]; then
    curl --silent --fail --resolve api.zovoeh.cn:443:127.0.0.1 https://api.zovoeh.cn/health/live >/dev/null
    echo 'Managed HTTPS is already active; use a reviewed reload for later changes.'
    exit 0
fi

# Backup has its own maintenance lock. Do not acquire it twice in one process.
/usr/local/sbin/goutou-backup
exec 9>/run/lock/goutou-maintenance.lock
flock -n 9 || { echo 'Another maintenance operation is running' >&2; exit 1; }
ufw status | grep -q '^Status: active$' || { echo 'Inspect firewall before activation' >&2; exit 1; }
if ufw status | grep -Eq '^(80|443)/tcp([[:space:]]|$)'; then
    echo 'Existing web-port firewall rules require review before first activation' >&2
    exit 1
fi
prior=$(mktemp -d /var/backups/goutou/https-activation.XXXXXX)
cp -p /etc/caddy/Caddyfile "$prior/Caddyfile"
was_active=0
systemctl is-active --quiet caddy && was_active=1
enabled_state=$(systemctl is-enabled caddy || true)
case "$enabled_state" in
    enabled|disabled) ;;
    *) echo 'Inspect existing Caddy unit enablement before activation' >&2; exit 1 ;;
esac
added_autostart=0
added80=0
added443=0
rollback() {
    status=$?
    trap - EXIT
    systemctl stop caddy || true
    cp -p "$prior/Caddyfile" /etc/caddy/Caddyfile || true
    rm -f -- /etc/goutou/https.json
    if [[ $added443 == 1 ]]; then ufw --force delete allow 443/tcp || true; fi
    if [[ $added80 == 1 ]]; then ufw --force delete allow 80/tcp || true; fi
    if [[ $added_autostart == 1 ]]; then systemctl disable caddy || true; fi
    if [[ $was_active == 1 ]]; then systemctl start caddy || true; fi
    echo 'HTTPS activation failed; previous Caddy config and web-port firewall state restored.' >&2
    exit "$status"
}
trap rollback EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
install -o root -g root -m 644 "$candidate" /etc/caddy/Caddyfile
runuser -u caddy -- caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
added80=1
ufw allow 80/tcp comment 'goutou-https'
added443=1
ufw allow 443/tcp comment 'goutou-https'
if [[ $enabled_state == disabled ]]; then
    added_autostart=1
    systemctl enable caddy
fi
systemctl restart caddy
ready=0
for attempt in $(seq 1 120); do
    if curl --silent --fail --connect-timeout 2 --max-time 4 \
        --resolve api.zovoeh.cn:443:127.0.0.1 https://api.zovoeh.cn/health/live >/dev/null; then
        ready=1; break
    fi
    sleep 1
done
[[ $ready == 1 ]] || { echo 'Trusted TLS certificate was not ready in time' >&2; exit 1; }
python3 - <<'PY'
import datetime, hashlib, json, os, pathlib, tempfile
manifest = dict(domain='api.zovoeh.cn', base_url='https://api.zovoeh.cn',
    upstream='127.0.0.1:8000', pocketbase_public=False, operator_public=False,
    activated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    caddyfile_sha256=hashlib.sha256(pathlib.Path('/etc/caddy/Caddyfile').read_bytes()).hexdigest())
fd, temporary = tempfile.mkstemp(prefix='.https-', dir='/etc/goutou')
try:
    with os.fdopen(fd, 'w') as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(json.dumps(manifest, indent=2)+'\n')
        output.flush(); os.fsync(output.fileno())
    os.replace(temporary, '/etc/goutou/https.json')
finally:
    pathlib.Path(temporary).unlink(missing_ok=True)
PY
trap - EXIT
echo 'Trusted client HTTPS active; PocketBase and operator API remain private.'
