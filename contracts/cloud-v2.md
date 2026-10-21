# Evidence and memory increment (Cloud v1 compatible)

Approved scope: personal Android/iOS/Windows use, PocketBase remains the fact store,
all business calls through the existing gateway. No vector/graph database, autonomous
actions, extra model call, or new provider Key. Existing cloud-v1 contract still applies.

## Preferences

The four fields are tone, length, count (integer 1..3), style. Missing fields in an
analysis override are merged over stored preferences, never replace the entire object.
Explicit empty style clears it. Clients preserve unknown preference fields on save.
iOS exposes style in its settings and reads/saves it. Draft prompt requests the chosen
count and style; server bounds returned candidates to count without inventing text.

## Per-analysis person and current intent (Windows/Android, 2026-10-21)

The review page explicitly selects a live profile or stages a new profile until
confirmation. Platform conversation bindings remain device-specific; the same
profile can own conversations from several platforms. Confirmed memories are
selected by profile, while raw messages remain scoped to the selected conversation.
Changing a binding affects future input and never reparents historical messages.
Blank, placeholder or transient titles must not recover/create persistent person
bindings. Explicitly selected unnamed reviews use an isolated conversation identity.

`POST /v1/analyses` accepts optional `current_intent` for `kind: "analysis"`:
a strict string, at most 2000 Unicode code points before trimming. Blank input is
omitted and keeps the legacy idempotency digest and persisted client request body.
Nonempty intent participates in the durable request fingerprint; editing it cannot
reuse a job for another intent. A network-ambiguous retry retains the exact original
body and request ID. Explicit task recovery resumes that original request.

PocketBase stores normalized `job.input.current_intent` with the immutable analysis
snapshot, not in profiles, notes, preferences or memories. Rewrite/explanation reads
it from the selected successful parent's input; client nonempty overrides are
rejected. Model result fields and newer analyses cannot replace the user's original
input. Legacy parents without the field contribute an empty string.

Evidence, drafting, ordering and followup prompts receive the same input. Stated
current state/goals take priority over stale profile goals and model guesses;
they are not chat messages or evidence of what the other person said. Existing
quote validation still accepts only actual immutable message text. There is no
additional model call or automatic long-term memory write. New review input starts
blank; keeping it with a durable analysis does not mean it is never stored.

This is additive and old clients may omit the field. iOS work and screenshot/OCR
changes are deferred. Data association does not add capture support for a platform.

## Evidence

Analysis results retain facts/hypotheses/unknowns string arrays for old clients and add
`evidence:[{claim_id,kind:"fact|hypothesis|unknown",text,source_message_uids:[],
source_quotes:[{message_uid,quote}],citation_status:"verified|unavailable"}]`.
UIDs/quotes are accepted only from the immutable confirmed-message job snapshot; quote
must be an exact substring of that message. Invalid references are removed. A claim with
no valid exact quote is explicitly unavailable, never given an invented citation.
Legacy model output without citations remains readable and is marked unavailable.

`GET /v1/analyses/{analysis_id}/evidence` (normal three session headers) returns
`{analysis_id,conversation_uid,profile_uid,context_revision,evidence:[],messages:[]}`.
Messages are the job snapshot `{uid,version,role,text,observed_at,order_key,...}`;
the view explains that this is the original analysis context, not later edits.
Only the same owner with a live session can read it. Cancelled/deleted context ->404.
No model generation. UI lists claims, source quotes, speaker and timestamp; missing
references display unavailable. Users may explicitly save a claim as pending memory.

## Memory write and confirmation

Use existing `memories` sync/outbox operations and optimistic versions, with payload
`{profile_uid,text,kind:"fact|hypothesis",status:"pending|confirmed",source_uids:[],
source_type:"messages|user",change_reason,source_refs:[{message_uid,version}]}`.
`source_refs` is server generated; clients must not treat client supplied versions as proof.
Text is nonempty <=4000 chars, reason <=500 chars, at most16 unique source UIDs.
Messages must be live, confirmed and belong to the same owner AND profile. Nonempty
sources imply source_type messages; no sources means manual user statement (source_type
user). Hypotheses cannot be confirmed; explicit conversion to fact is a separate user
choice. New memory defaults pending. Clients expose create, edit/correct (reason), confirm
fact, return to pending, delete (confirmation), and view history. Creating from a claim
always uses pending and only sources in that account/profile.

Editing a referenced message automatically returns affected memories to pending and adds
a history version explaining why. Deleting a source message removes derived memories and
analyses/jobs containing that message, including old sync snapshots, to prevent recovery
of deleted text through another view. No automatic merging of people or model auto-confirm.
Changes to the effective confirmed memory set advance every live conversation context_revision
for that profile, so existing client job caches cannot silently reuse analysis of old memory.
Privacy deletion follows explanation/rewrite parent ancestry, including v1 parent snapshots.

## Memory history

`GET /v1/memories/{uid}/history?before_version=0&limit=50` returns
`{memory_uid,profile_uid,versions:[<cloud-v1 entity record>],has_more,next_before_version}`,
newest first, max50/page; 0 means newest. Owner and session checked. Deleted/unknown
memory ->404 (no deleted-content restore). History uses existing transactional cloud_changes.
UI can restore a selected version by staging an ordinary update against the CURRENT
version, with status pending and change_reason `恢复版本 N`. It never rewinds sync history.
Sources are revalidated at write time; old content cannot bypass deletion rules.
History needs network; offline edits remain in the normal outbox. Display local pending
and server confirmed states accurately. Account changes close/clear evidence/history views
and late responses cannot mutate another account or display the prior account's data.

## Verification and rollout

Real isolated PocketBase tests cover cross-account/profile references, same-op replay,
history pagination and optimistic conflicts, source edits/deletes and old receipt privacy.
Pipeline tests use fake models; no paid call. Three clients test preference preservation,
evidence mapping and memory lifecycle/recovery. Existing v1 results remain readable.
No schema migration required. Private server rollout must first take a consistent backup;
The personal deployment enabled trusted HTTPS on 2026-10-21; the older cutover
stage is recorded separately. iOS work is deferred: its remote simulator/unsigned
build is validation, not Apple signing or actual-device acceptance.
