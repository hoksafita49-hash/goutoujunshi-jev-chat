#!/usr/bin/env bash
# Cold snapshot avoids copying a live SQLite database. Root-only, not offsite backup.
set -euo pipefail
umask 077
[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
exec 9>/run/lock/goutou-maintenance.lock
flock -n 9 || { echo "A deployment or backup is already in progress." >&2; exit 1; }
install -d -m 700 /var/backups/goutou
pb_was_active=0
gateway_was_active=0
caddy_was_active=0
backup_paths=(var/lib/goutou/pocketbase etc/goutou opt/goutou/deployment.json)
systemctl is-active --quiet goutou-pocketbase && pb_was_active=1
systemctl is-active --quiet goutou-gateway && gateway_was_active=1
# Older deployments have no Caddy unit or TLS storage.
if systemctl cat caddy.service >/dev/null 2>&1; then
    backup_paths+=(etc/caddy var/lib/caddy)
    if systemctl is-active --quiet caddy; then caddy_was_active=1; fi
fi
partial=""
restore_services() {
    status=$?
    trap - EXIT
    # A failed current archive is never useful and must not consume remaining disk.
    if [[ -n "$partial" && -f "$partial" ]]; then rm -f -- "$partial" || status=1; fi
    if [[ $pb_was_active -eq 1 ]]; then systemctl start goutou-pocketbase || status=1; fi
    if [[ $gateway_was_active -eq 1 ]]; then systemctl start goutou-gateway || status=1; fi
    if [[ $caddy_was_active -eq 1 ]]; then systemctl start caddy || status=1; fi
    exit "$status"
}
trap restore_services EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# Pause TLS storage writes before taking the cold snapshot.
if [[ $caddy_was_active -eq 1 ]]; then systemctl stop caddy; fi
systemctl stop goutou-gateway
systemctl stop goutou-pocketbase
stamp=$(date -u +%Y%m%dT%H%M%SZ)
archive="/var/backups/goutou/goutou-${stamp}.tar.gz"
partial="${archive}.partial"
tar -czf "$partial" -C / "${backup_paths[@]}"
tar -tzf "$partial" >/dev/null
mv "$partial" "$archive"
partial=""
chmod 600 "$archive"
# Only this script's completed archives in this fixed directory are eligible.
mapfile -t archives < <(find /var/backups/goutou -maxdepth 1 -type f \
    -name 'goutou-????????T??????Z.tar.gz' -printf '%f\n' | sort -r)
for ((i=7; i<${#archives[@]}; i++)); do
    rm -- "/var/backups/goutou/${archives[$i]}"
done
echo "Local backup created: $archive"
