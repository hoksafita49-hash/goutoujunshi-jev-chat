# Windows and Android live acceptance — 2026-10-21

Historical record from before the HTTPS cutover. The tunnel/USB requirements and
network limits below describe that earlier run. Daily access now uses
`https://api.zovoeh.cn`; see the [HTTPS cutover record](https-cutover.md) and the
user-facing `HTTPS接入与验收记录.txt` for current access and physical-device checks.

This follow-up exercises the packaged Windows EXE and the actual USB-connected
Android 16 device against the existing PocketBase account and gateway. Only a
labeled synthetic person and four manually confirmed coffee-planning messages
were used. Capture remained paused; no candidate was sent to a contact.

## Fixes discovered by installed-app testing

- PyInstaller had collected an incompatible `icuuc.dll` from the build host.
  It shadowed Windows ICU and prevented QtCore from loading. The spec now excludes
  that basename. `check_bundle.py` loads the six Qt modules in an isolated native
  process and records the resolved ICU path (System32 in both final checks);
  `build.bat` runs this gate.
- An already-open Windows style form did not refresh after remote sync. Successful
  sync now refreshes an untouched form while preserving an unsaved local draft.
  An unchanged form cannot overwrite a newer remote style; explicit clearing
  remains supported. Four real-Qt regressions cover these cases/account changes.
- A real model task failed with `invalid_model_output`. One controlled diagnostic
  with the same synthetic snapshot and original prompt reproduced valid JSON with
  `strategy: {action, stop_condition}`. The original failed raw response was not
  stored. The gateway now specifies top-level string fields and canonicalizes
  only that exact nested shape when no top-level stop condition exists. Ambiguous
  or unknown shapes still fail before drafting. There are no paid request retries.

## Observed results

- Both installed apps use the same existing account. Android was updated in place
  with `adb install -r`; no uninstall/data clearing was performed.
- Android's tone/length/count/style changes reached Windows; a Windows-only style
  change reached Android without changing the other three fields.
- With the Windows settings page kept open, Android style A appeared without
  reopening. After typing a Windows draft, Android style B reached the local
  mirror while the unsaved draft stayed visible. Reopening showed saved style B.
- Windows manually created one pending memory. Android saw cloud version 1;
  its separate confirmation produced version 2, which Windows displayed as
  server-confirmed. Android correction produced version 3/pending. Both native
  clients read the three historical versions; reasons and prior statuses were
  retained. Unsynchronized creation could not be directly confirmed on Windows.
- The repaired real analysis succeeded: one candidate, four evidence claims,
  three claims with verified quotes, context revision 6. The candidate was
  `好，美式记下了，周六见。`. Windows displayed original quotes, speaker, time,
  message UID and version from the analysis snapshot. Android independently
  displayed the coffee quote and complete original-message snapshot.
- Android saved the quoted coffee fact once as a new pending memory, cloud version
  1. Both native clients displayed its original-message source. It was not
  automatically confirmed. Android also verified the restored default preferences
  after the final sync.

## Validation and deployment

- Server/full Python suite: 303 passed, 89 subtests passed, 14 skipped. Thirteen
  skips require Qt; one integration test needs port 18000 currently used by the
  authorized live tunnel. Windows environment separately passed 240 tests,
  including the Qt cases. Counts overlap and must not be added.
- New provider-response contract suite: 11 passed, including exact observed shape,
  conflicting stop conditions, malformed JSON/truncation, and no automatic retry.
- Corrected Windows package: all 5,878 ZIP entries passed CRC; 36 embedded modules
  and 603 recursive code objects matched source. Native Qt load gate passed on
  both the build directory and the final extracted installation.
- Reviewed server source deployed after a successful backup. Private fingerprints
  verified that provider Key/configuration and account credentials were unchanged.
  Ready check passed; gateway and PocketBase retain loopback listeners.
- Independent focused review reported no remaining P1/P2; diff check passed.

## Cost and practical limits

Two full analysis tasks were explicitly requested: the first failed, the repaired
one succeeded (4,591 tokens). One single-stage diagnostic used 1,345 tokens.
The failed task did not retain usage, so a complete bill cannot be calculated
from these figures. Reading evidence/history and memory/preferences operations
did not request model generation.

The pre-test preference row was absent. Effective defaults were restored through
the official client sync path: natural/short/3/empty style; the fixed preference
UID was not deleted. Labeled synthetic records are retained for review.

Current access requires the Windows SSH tunnel and Android USB reverse forwarding.
Domain/TLS, independent mobile Internet access, iPhone signing/installation,
real-chat OCR permissions and historical restore/deletion via the installed UI
were outside this follow-up. Existing automated restore/deletion tests do not
count as physical-device acceptance of those UI flows.
