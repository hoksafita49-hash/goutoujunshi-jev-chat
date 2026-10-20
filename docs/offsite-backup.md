# Encrypted offsite backup and isolated recovery

This extension preserves the existing PocketBase/gateway/Caddy cold-backup flow.
Windows and Android remain the maintained client scope; iOS is deferred. It does
not change client databases, model settings, DNS or public routes.

## Storage and credentials

- Dedicated private OSS bucket: `zovoeh-backup-20261021`, Beijing (`cn-beijing`).
- Public HTTPS endpoint: `https://oss-cn-beijing.aliyuncs.com`.
- Managed repository prefix: `goutou-v1/`; restic encrypts backup content before
  rclone uploads through its documented Alibaba provider.
- Use the dedicated programmatic RAM user and `deploy/offsite/oss-ram-policy.json`.
  No account administrator or `AliyunOSSFullAccess` permission is needed. Delete
  permission is restricted to this repository and supports locks/retention.
- Keep Bucket public access blocked, ACL private, Standard/LRS. SSE-OSS AES256 is
  an additional server-side layer; it does not replace restic encryption.

`/etc/goutou-offsite/config.json` is root-only mode0600 inside a root-only mode0700
directory. It holds the RAM credentials and an independent repository password.
This directory is outside the existing `/etc/goutou` cold-snapshot set, so cloud
credentials and repository password are not included in their own backup.

The owner chooses and separately retains a repository password of at least16
characters. It must remain available after losing either VPS or Windows computer.
Store it in a personal password manager or independent offline recovery record.
Possessing the encrypted objects without that password is insufficient to restore.
Never put it, AccessKey Secret or application passwords in chat, command arguments,
Git, source archives, scripts or screenshots.

## Installation and first activation

1. Review sources, then run `bash deploy/install-offsite.sh` on the managed Ubuntu
   24.04 amd64 VPS. It installs signed Ubuntu restic/rclone packages if absent,
   prepares hashed Python3.12 wheels from the exact live lock, installs versioned
   backup helpers and service definitions. It does not enable the upload timer.
2. Create/authorize the dedicated RAM identity with the policy above. The Windows
   `configure-offsite-backup.cmd` wrapper opens hidden credential inputs. Each
   input is checked immediately, and only an invalid field needs repeating.
   `powershell.exe -STA -File configure-offsite-backup.ps1 -ImportCsv` instead
   opens a native file picker for the owner's saved RAM CSV. The local process
   extracts one key pair from the selected file; it never sends the CSV itself,
   prints its contents or searches for credential files. The independent backup
   password is still entered privately and retained separately by the owner.
   Verified
   SSH receives bounded JSON on stdin; provider output is suppressed. It validates
   OSS access and either opens the existing encrypted repository or initializes a
   provably empty prefix. Failed checks preserve previous local credentials.
3. Run `/usr/local/sbin/goutou-backup` if no recent completed local snapshot exists.
   Wait for services to resume and private readiness. Then run
   `/usr/local/sbin/goutou-offsite-backup` for the first actual encrypted upload.
4. Download/decrypt that specific snapshot and perform the isolated recovery below.
   Only after those checks pass, enable the recurring timer:
   `systemctl enable --now goutou-offsite-backup.timer`.

Existing repository passwords cannot be silently replaced by the setup tool.
Credentials are only saved after repository validation. `--check` on the setter
or backup helper reports local readiness/status without contacting OSS or uploading.

## Schedule, consistency and cost controls

The existing daily local cold backup starts04:20UTC (China12:20), with up to15min
jitter. It keeps7 completed archives. The offsite timer starts04:40UTC (China12:40),
with up to5min jitter, and catches up after downtime. Offsite transfer runs after
local snapshot publication and service recovery; it never holds services stopped
while waiting for OSS.

The uploader validates a regular root-only completed archive, full tar readability,
age at most30h and the recorded release under `/opt/goutou/releases`. A short shared
maintenance lock captures matching runtime materials, then releases before network
work. A separate nonblocking offsite lock prevents concurrent setup/upload attempts.

The bundle contains the cold archive, exact gateway/PB source, pinned offline
wheelhouse, PB0.40.5 and Caddy binaries, service definitions and a SHA256 file map.
The wheelhouse is bound to the exact release lock by checksum. Recovery therefore
does not depend on the old VPS release directory or a working PyPI connection.

Safeguards include a256MiB staged-bundle limit, at most one successful automatic
upload per UTC date, replay/digest suppression, bounded subprocess durations and
256KiB/s upload limiting for both backup and pruning. Each prune run repacks at
most64MiB of selected pack input, within its existing900s timeout; remaining work
can wait for later runs. Successful uploads retain7daily,4weekly and3monthly
snapshots grouped by the dedicated host/tag. Pruning follows a successful upload;
upload failure does not remove prior remote snapshots or local cold backups.
Retention failure is reported separately from the successfully recorded upload.
Root-only `--force` is for explicit maintenance/verification, not automatic retries.

These controls reduce accidental use; they do not impose an OSS billing hard cap.
Storage, API requests and download traffic are separately metered. Stopping new
uploads does not stop charges for already-stored objects. Configure cloud budget
notifications separately if desired, and do not treat account balance as a cap.

## Isolated recovery check

Use restic with private configuration to select/download the exact saved snapshot
into a new root-only directory. Verify repository integrity and restored hashes.
Pass its restored bundle to the helper under a fresh network namespace:

```sh
unshare --net sh -c 'ip link set lo up; exec /usr/local/sbin/goutou-verify-offsite-restore --bundle /private/restored/bundle'
```

The helper refuses the host network namespace or a namespace with non-loopback
interfaces. It checks bundle/member hashes, safe paths and SQLite integrity;
rebuilds a fresh gateway venv using only included wheels; starts copied PB/runtime
on isolated loopback; verifies account login, pulled data, memory histories,
analyses and evidence. Epoch/session invalidation affects only the restored copy.
The copied gateway has workers disabled and no configured model routes. No model
generation, real user message transmission, ACME request or production reset occurs.

Certificate/key pairs are parsed from the copy. This check does not run a public
TLS listener, alter DNS, test Android/Windows against the restored copy, or replace
the documented operational steps for an actual replacement VPS. Preserve and
review the helper's precise JSON report rather than calling it a full failover.

## Real replacement VPS

Provision matching Ubuntu/Python prerequisites and obtain the independent backup
password plus narrowly-scoped OSS access. Download/decrypt, verify the bundle,
rebuild from included matching runtime, restore root/app ownership and private
configuration. Keep public Caddy/gateway stopped during restore. Start copied PB,
rotate sync epoch using the documented restore command, then start/verify gateway
before enabling Caddy/public DNS. Old clients must log in again and explicitly
review frozen recovery queues; never silently replay old pending model tasks.

The original VPS's cloud credentials can be revoked/replaced independently. If
recovering expired TLS material, obtain a new trusted certificate through Caddy
rather than bypassing certificate validation on clients.

## Verified deployment — 2026-10-21

The owner explicitly authorized encrypted production snapshots to the dedicated
OSS prefix, actual readback/recovery testing, and daily activation. First upload
completed at 2026-10-21 06:54:24 Asia/Shanghai, using the completed local archive
`goutou-20261020T213635Z.tar.gz`. Snapshot ID:
`9a1172ae304cafce9468697830c52828aa46397d7988546efe81af3d751ff2f6`.
Encrypted repository storage was approximately 36.6 MB at acceptance.

The actual OSS repository passed full-data integrity checking. The exact snapshot
was downloaded, decrypted and hash-verified, then recovered in an isolated network
namespace using a fresh offline gateway venv. The durable report passed at
2026-10-21 07:19:15 Asia/Shanghai: 40 bundle files, 22 snapshot files, 1 owner,
12 entities, 28 changes, 2 jobs, 2 evidence records and 4 memory-history versions. Owner
login, gateway pull, restored-copy epoch rotation and old-session rejection passed.
One certificate/key pair was parsed; anonymous access to a real repository object
returned 403. Workers were disabled and no model requests were made. Temporary
plaintext copies and verifier services were cleaned.

`goutou-offsite-backup.timer` is enabled/active; its first next trigger was
2026-10-21 12:44:36 Asia/Shanghai (04:44:36 UTC). The existing local timer remains
enabled/active. Private readiness and public trusted HTTPS returned 200; PB,
gateway and Caddy remained active. Credential file/directory modes 600/700 were
checked without displaying their contents.

The result verifies recovery from an OSS copy on the existing VPS. It does not
claim replacement-VPS failover, a restored public TLS listener or client testing
against the restored copy. Sources were checked with 178 passing backend/deployment
tests, 2 existing skips and 34 subtests; one existing Starlette warning remains.

References: [restic repositories](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html),
[restic restore](https://restic.readthedocs.io/en/stable/050_restore.html),
[rclone Alibaba provider](https://rclone.org/s3/#alibaba-oss),
[OSS RAM prefix policies](https://help.aliyun.com/zh/oss/user-guide/access-control-base-on-ram-policy),
[OSS server-side encryption](https://help.aliyun.com/zh/oss/user-guide/server-side-encryption-8).
