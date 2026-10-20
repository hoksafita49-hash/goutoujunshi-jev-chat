# Trusted HTTPS cutover — 2026-10-21

Endpoint: `https://api.zovoeh.cn`. The owner completed domain identity verification
and created the A record pointing to the existing VPS. Authoritative DNS, an
independent resolver and the VPS agreed before activation.

## Server validation

- Official signed stable Caddy package v2.11.7. Reviewed candidate passed native
  `caddy validate` before web-port firewall changes; activation also validated as
  the Caddy service user.
- External TLS from Windows verified the public trust chain and hostname with
  verification enabled, negotiated TLS1.3, and read the certificate expiry
  `Jan 18 20:28:35 2027 GMT`. Renewal is managed by Caddy.
- Public health/live200; unauthenticated config/devices401; operator chat, PB,
  private health/ready, docs and unsupported methods404. A262145-byte login body
  returned413; plain HTTP returned308 to the same HTTPS hostname/path.
- Only TCP22/80/443 are allowed inbound. Gateway8000, PB8090 and Caddy admin2019
  listen on loopback. No provider/account secret changed (private fingerprints
  compared before/after); no DNS, account or provider credential was printed.
- Request/access error logs that could contain custom device credentials are
  excluded. Proxy upstream keepalive4s matches Uvicorn's5s idle limit; no retry
  policy was added. Existing global gateway rate limits remain, without new per-IP
  or DDoS protection claims.

## Data-preserving client handoff

Old-address Windows sync completed with zero pending/conflicts/unresolved jobs.
The actual Android app separately synced the old localhost endpoint with zero
pending/conflicts/rejected operations before its URL changed. Old caches were
preserved; no data clearing or unresolved job resubmission was used.

Windows used the official client to authenticate the same owner at HTTPS and save
the new session using DPAPI. Old/new mirrors matched exactly:1person,1conversation,
4messages,2memories,2analyses,1preference record. The existing successful analysis
returned4source messages/3verified claims; the corrected memory's3history versions
were readable over HTTPS. The installed EXE was restarted and its native settings
showed HTTPS, the same owner, and zero pending/conflicts/rejected operations.

Android same-owner HTTPS login/sync succeeded using the native app and Keystore;
it read the same synthetic person,2memories,2analyses and the3-version correction
history. The deployment's USB reverse on port18000 was then removed and a fresh
native HTTPS sync succeeded with zero pending/conflicts/rejections. Read-only
ConnectivityService inspection showed CELLULAR as the default transport. USB was
still physically connected during that observed check. On2026-10-21 the owner
then reported successful sync after following the requested physical USB unplug,
Wi-Fi off and mobile-data-only steps. This final physical check is owner-reported,
separate from the tool-observed checks above. Android's native UI
does not expose a total message count, so no phone-cache message-count equality
claim is made. The native default preferences also matched natural/short/3/empty
style without editing them.

No new model generation was requested during this cutover. Existing test results
were read rather than regenerated. Real-chat OCR and physical iPhone installation
remain outside this stage; iOS still requires distribution signing and real-device
acceptance.

## Backup and rollback

Pre-install backup: `goutou-20261020T212107Z.tar.gz`.
Pre-activation backup: `goutou-20261020T212658Z.tar.gz`.
Post-cutover backup: `goutou-20261020T213635Z.tar.gz`. The archive was readable,
root-owned mode0600, and included PB data, Caddy configuration, issued TLS private
material and the HTTPS manifest. All three services and the backup timer were
active afterward; private readiness and external trusted HTTPS checks passed.
An immediate first readiness probe ran before the gateway had bound its socket;
a separate follow-up confirmed startup success. The verification helper now
waits for readiness instead of assuming systemd process-start means ready.
At the HTTPS-cutover stage, backups remained root-only on the same VPS and
offsite backup had not yet been implemented. Update on 2026-10-21: encrypted OSS
upload, actual download/decryption and same-VPS isolated recovery passed, and
daily offsite backup is enabled. See [offsite acceptance](offsite-backup.md#verified-deployment--2026-10-21)
for the evidence and the limits of this recovery test.

Cold backup now pauses an originally-active Caddy and includes configuration/TLS
storage, then restores original activities after PB/gateway. Fourteen behavior
tests cover success, absent/inactive Caddy, stop/start/archive/cleanup failures,
locking and retention. Three activation tests exercise certificate-wait failure,
configuration/firewall restoration, preservation of enabled/active states and
cleanup of a newly-created HTTPS marker after failure. Tests use real Bash with
fake commands and isolated filesystems; they are not live disaster-recovery tests.

Combined gateway/PocketBase/deployment verification completed with93passed,
1skipped and17subtests passed. The existing skip protects port18000 occupied by
the acceptance tunnel; an existing Starlette deprecation warning remains. Client
executables were unchanged in this HTTPS stage; only their runtime server/session
configuration changed. Historical client-build checks are not added to this
test total.

For intentional rollback, first sync current HTTPS clients and retain their
address-specific caches. Review the saved Caddy config and firewall state, stop
the public edge, and use the retained SSH path for the unchanged backend. Merely
stopping the edge does not rewind cloud data or move pending local operations.

References: [Caddy installation](https://caddyserver.com/docs/install),
[automatic HTTPS](https://caddyserver.com/docs/automatic-https),
[request limits](https://caddyserver.com/docs/caddyfile/directives/request_body),
[reverse proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy).
