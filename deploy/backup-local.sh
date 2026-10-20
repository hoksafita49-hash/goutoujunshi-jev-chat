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
systemctl is-active --quiet goutou-pocketbase && pb_was_active=1
systemctl is-active --quiet goutou-gateway && gateway_was_active=1
partial=""
restore_services() {
    status=$?
    trap - EXIT
    # A failed current archive is never useful and must not consume remaining disk.
    if [[ -n "$partial" && -f "$partial" ]]; then rm -f -- "$partial"; fi
    if [[ $pb_was_active -eq 1 ]]; then systemctl start goutou-pocketbase || status=1; fi
    if [[ $gateway_was_active -eq 1 ]]; then systemctl start goutou-gateway || status=1; fi
    exit "$status"
}
trap restore_services EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
systemctl stop goutou-gateway
systemctl stop goutou-pocketbase
stamp=$(date -u +%Y%m%dT%H%M%SZ)
archive="/var/backups/goutou/goutou-${stamp}.tar.gz"
partial="${archive}.partial"
tar -czf "$partial" -C / \
    var/lib/goutou/pocketbase etc/goutou opt/goutou/deployment.json
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
