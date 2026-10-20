# Cloud v2 verification — 2026-10-21

Historical verification record from before installed-app acceptance and HTTPS
activation. Its counts and boundaries describe that increment. Daily access now
uses `https://api.zovoeh.cn`; see the [HTTPS cutover record](https-cutover.md) and
the user-facing `HTTPS接入与验收记录.txt` for the latest handoff.

This increment uses the existing PocketBase account/sync service and unified
model gateway. The additive contract is in `contracts/cloud-v2.md`.

## Delivered behavior

- Android, iOS and Windows preserve tone, length, candidate count, style and
  unknown preference fields. Partial request overrides preserve other fields.
- Evidence identifies exact original message quotes and versions in the analysis
  snapshot. A matched citation does not establish that an inference is true.
- Memory supports explicit pending/confirmed facts, pending hypotheses,
  corrections with reasons, paged history and restoring into a new pending
  revision. Unsynced edits must synchronize before a separate confirmation.
- Source corrections invalidate affected confirmed memories. Confirmed memory
  changes advance conversation context revisions. Source deletion scrubs affected
  live server history/derived jobs and synchronized local task caches; canceled
  tasks cannot silently trigger another paid request.

## Verification performed

- Final Python suite: **293 passed, 89 subtests passed**. Nine Qt tests skipped in
  the server environment; the separate Windows environment ran **236 tests**
  successfully including those Qt tests. These suite counts overlap.
- Android: **92 tests, 18 suites, zero failures/errors/skips**; offline Gradle
  build succeeded and APK signature v2 verified. See
  `integrations/jev_android/BUILD_CLOUD_V2.md`.
- Windows: clean PyInstaller build; all **36 embedded modules / 602 code objects**
  matched frozen source (only file paths excluded). All 5,878 ZIP entries passed
  CRC. Bundle includes a detailed `SOURCE-VERIFICATION.json`.
- Independent review: material findings were fixed and final focused review
  reported no remaining P1/P2. Tests include real local SQLite/PocketBase state,
  source/version isolation, late responses, conflict handling and deletion.
- Private VPS: backup before/after upgrade; ready check and service/timer state
  verified. Synthetic three-platform account sessions exercised evidence,
  confirmation, correction, history/restore, source changes/deletion and revocation.
  Synthetic records were removed. Provider configuration was preserved. Services
  remain loopback-only behind the existing SSH access path.
- iOS: [run 39485965448](https://github.com/hoksafita49-hash/goutoujunshi-jev-chat/actions/runs/39485965448)
  on `c500dca95b912745768ef4baea15a422bba5e079` passed all **308 Swift checks**
  (core 165, typing 33, image 27, cloud SQLite 83), the complete simulator build
  (app, keyboard and Capture extension), and unsigned iPhone device compilation.
  Actual typechecking exposed an implicit `catch error` shadowing a view state;
  the one-line fix was reverified by the full successful rerun. Syntax parsing
  alone was insufficient.
  Downloaded simulator archives passed CRC, iPhoneSimulator platform, both
  x86_64/arm64 architectures and executable permission checks for all three
  bundles. See `docs/ios-cloud-v2-build.md` for the actual toolchain and checksum.

## Boundaries

No additional paid model generation was used for this increment's verification.
Domain/HTTPS activation is deferred. Apple distribution signing, iPhone install,
actual Android installation/OCR permissions and three physical-device continuity
have not been accepted. Windows testing used Qt fixtures and inspected the bundle;
the packaged EXE was not launched against real chats.

Deletion is not physical secure erase. Offline devices that have not synchronized,
old exports/screenshots, recovery/conflict copies and prior backups require their
holders to manage them separately. Backups currently remain on the same VPS.

## Follow-up installed-app acceptance

The later Windows/actual-Android follow-up found and fixed a bundled ICU loading
failure, an open-form preference refresh issue, and a reproducible provider JSON
shape mismatch. That follow-up includes real paid generation, rather than only
fake-provider tests. See [the live acceptance record](windows-android-live-acceptance.md)
for the separate observations, updated verification counts and remaining limits.
