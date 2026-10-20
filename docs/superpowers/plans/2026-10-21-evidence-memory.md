# Evidence and Memory Implementation Plan

> For agentic workers: use subagent-driven-development for independent clients and root implementation of the shared backend; review the whole branch before delivery.

**Goal:** Preserve cross-platform preferences and make evidence and memory user-reviewable.
**Architecture:** Additive cloud-v1-compatible gateway views; transactional PocketBase validation/history; existing SQLite/outbox and platform UI.
**Tech Stack:** PocketBase JS hooks, Python FastAPI, Python/Qt, Kotlin/Android, Swift/iOS.
**Spec:** `contracts/cloud-v2.md` (user authorized the preceding concrete recommendations and asked to start).

## Global Constraints

- No new infrastructure/dependencies or paid model test.
- No implicit direct-key fallback; existing account/epoch/outbox guards apply.
- Hypotheses never auto-confirm; restoring a revision produces a new pending revision.
- Privacy deletion takes precedence over revision recovery.

## Review Focus

- A missing override field must preserve saved preferences; empty style clears intentionally.
- Cross-person/account citation and late callback must not leak data.
- Concurrent/offline memory changes must surface the existing conflict rather than overwrite.
- Referenced message correction invalidates confirmed memory; deletion scrubs derived/history data.
- Legacy analyses without evidence remain readable and show citation unavailable.

## Tasks

- [x] Backend: reproduce preference replacement and add merge validation; bound draft count.
- [x] Backend: pipeline evidence normalization, authenticated snapshot view, fixture.
- [x] Backend: memory validation, history pagination, source mutation/deletion behavior.
- [x] Windows: preference transmission fix, evidence and memory controls, lifecycle/Qt checks.
- [x] Android: evidence and memory controls, lifecycle/SQLite checks, debug APK build.
- [ ] iOS: style preservation/editor, evidence and memory controls, shared checks and remote build.
- [x] Integration: real PocketBase+gateway, source isolation/conflict/deletion regression.
- [x] Review: independent full-branch review, repair material findings, run required suites.
- [ ] Delivery: private backup/deployment verification, updated source/build packages and guide.

Each implementation task first adds/runs a failing behavioral regression, then implements
and reruns its relevant suite. Backend and client implementation share only the frozen
spec; clients do not change server files. Root owns final integration and deployment.

## Verification record

- Python/PocketBase/gateway/deploy: 293 tests and 89 subtests passed; 9 Qt tests
  skipped in the server-only environment. The separate Windows environment ran
  236 tests successfully, including those Qt checks; these counts overlap.
- Android: 92 tests across 18 suites, no failures/errors/skips; debug APK built
  and its v2 signature verified. See `integrations/jev_android/BUILD_CLOUD_V2.md`.
- Independent review repaired legacy descendant-job deletion, old memory snapshot
  deletion, conversation cache invalidation, version monotonicity, unsynced memory
  confirmation, and local deleted-job cache/replay issues. Final focused review
  found no remaining P1/P2 issue.
- Private VPS: pre/post-upgrade backups, active services, loopback listeners and
  ready check verified. Synthetic three-platform sessions exercised evidence,
  memory correction/history/restore, isolation, deletion and revocation without
  model generation. Provider settings were preserved.
- iOS latest remote compilation and Windows final archive verification remain
  delivery gates. Apple distribution signing and actual three-device acceptance
  are separate, unfinished work; domain/HTTPS is deferred as requested.
