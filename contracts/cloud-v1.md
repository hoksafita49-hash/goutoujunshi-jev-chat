# Cloud v1 implementation contract

Base URL: `https://api.zovoeh.cn` after TLS activation; during development ONLY
`http://127.0.0.1:18000` / `http://localhost:18000` (SSH tunnel). Reject other HTTP
origins, credentials/query/fragment in URLs, and redirects. Never ship vendor Keys
or the private operator bearer in clients. Direct provider mode remains explicit.

All JSON uses snake_case, UTC ISO8601, UUID entity/op/request IDs (lowercase UUID
strings preferred). Unknown response fields are ignored. Error shape:
`{"error":{"code":"..."}}`. No automatic paid-generation retries.

## Account / device authentication

`POST /v1/auth/login` body `{email,password,device_name,platform}`; platform is
`android|ios|windows`. No public registration. Initial account provisioned on server.
Success 200:
`{api_version:1, token:"PB user JWT", user:{id,email}, device:{id,name,expires_at},
device_secret:"random secret", server_epoch:"UUID"}`.

Protected headers: `Authorization: Bearer <token>`,
`X-Device-Session: <device.id>`, `X-Device-Secret: <device_secret>`.
Use OS protected credential storage; server URL is not secret. Never sync credentials.

`POST /v1/auth/refresh` (no body) validates BOTH credentials and returns `{token}`.
`POST /v1/auth/logout` revokes current device; clear local session and account view.
`GET /v1/devices` -> `{devices:[{id,name,platform,expires_at,revoked_at}]}`.
`DELETE /v1/devices/{id}` revokes own device; all subsequent requests denied.
401 -> preserve outbox/cache per account but require login, never silently fall back.

`GET /v1/config` -> `{api_version:1,min_client_version:1,sync_ready:true,
model_ready:true|false,server_epoch:"UUID",supported:[...],sync:{push_limit:50,pull_limit:200}}`.

## Entity record and payloads

Record `{entity_type,uid,version,created_at,updated_at,deleted_at,payload}`.
`deleted_at` null when live. Server derives owner; no request may set owner.
Types and payloads (fields below are the common cross-platform spelling):

- `profiles`: `{name,stage,goal,background,notes}`.
- `conversations`: `{profile_uid,title,platform,context_revision}`; server owns
  context_revision, incremented on confirmed message changes.
- `messages`: `{conversation_uid,profile_uid,role:"me|other|unknown",text,
  confirmed:true,observed_at,order_key?:"stable lexical conversation order"}`.
  Keep observed_at factual; order_key preserves confirmed ordering. Never upload
  unconfirmed OCR or keyboard handoff.
- `notes`: `{title,body,tags:[],enabled:true,always_on?:true,auto_summary?:false}`
  (general notes, not person facts).
- `memories`: `{profile_uid,text,kind:"fact|hypothesis",status:"pending|confirmed",
  source_uids:[]}`; never auto-confirm hypotheses.
- `preferences`: `{tone,length,count,style}`; fixed UUID per account is
  `00000000-0000-4000-8000-000000000001`.
- `identity_links`: `{profile_uid,platform,legacy_id,source_device}`; do not merge by name.
- `feedback`: `{analysis_uid,candidate_id,event,text}`; copy != sent.
- `analyses`: server writes immutable result/job payload, clients read only.

## Sync

`POST /v1/sync/push`: `{server_epoch,operations:[{op_id,entity_type,entity_uid,
base_version:0,action:"upsert|delete",payload:{...}}]}` (max 50).
200 `{server_epoch,results:[{op_id,status:"applied|conflict|rejected",record:<record>,
code?:"...",seq?:N}]}`. On conflict record is current remote version. On rejection
record may be absent. Applied ops only removed from outbox; preserve rejected/conflict.
Create base_version 0; updates/deletes require exact last server version.
Same device/op_id + same canonical contents returns original receipt, no new version.
Same op_id + different contents rejected. Updating tombstone cannot resurrect it.
Parent must exist/live and belong to account. Batch applied in supplied order.

`GET /v1/sync/pull?cursor=<opaque>&limit=200`; omit/empty cursor for first page.
200 `{server_epoch,changes:[{seq,record}],cursor:"opaque",has_more:false}`.
Cursor is account-bound, epoch-bound and freezes a high-water bound per scan.
Apply each page AND advance cursor in one local transaction. Never advance pull
cursor from push receipts. Local dirty edits remain visible above remote mirror.
Store conflicts as both local and remote; provide explicit user choice to resolve.
409 `server_epoch_changed`: preserve a local recovery copy/outbox, require login;
do not erase or automatically upload pre-restore cache. Logs/tombstones retained.
Polling while foreground every 15s; manual refresh; exponential backoff; single sync.

## Analysis jobs

First sync profile/conversation/confirmed messages, then
`POST /v1/analyses`: `{request_id,conversation_uid,context_revision,
kind:"analysis|explanation|rewrite",parent_analysis_id?:"UUID",selected_text?:"...",
preferences?:{tone,length,count,style}}`.
202 `{analysis_id,status:"queued",conversation_uid,context_revision}`.
`GET /v1/analyses/{id}` -> `{analysis_id,status,conversation_uid,context_revision,
result:null|<result>,error:null|{code}}`.
Same request_id + same contents returns same job; changed contents ->409.
Statuses queued/running/succeeded/failed/indeterminate/cancelled. Polling never bills.
Do not create a new request_id automatically after network ambiguity. Persist pending ID.
Results are also `analyses` sync changes; delayed result must match selected profile,
conversation, context revision and current account before changing visible UI.

Result: `{schema_version:1,analysis_id,conversation_uid,context_revision,
facts:[],hypotheses:[],unknowns:[],strategy:"...",stop_condition:"...",
candidates:[{candidate_id,text,reason:null|string,weight:null|number}],
ranking_status:"available|unavailable",usage:{...},pipeline_version:"cloud-v1"}`.
Preserve server order and IDs. Null weights must not become fake 0% probabilities.
Explanation/rewrite additionally return `text`. No image endpoint in this phase:
cloud mode uses existing on-device OCR or manual confirmed text and never falls back
to a device vendor Key. Backend fixed evidence -> draft -> rank pipeline.

## Local migration / accounts

SQLite per account; content + outbox atomic; stable legacy_id_map; never delete originals.
Import only after login + explicit preview/confirmation. Preserve identical short messages.
Account switch cannot expose/upload previous user's content. iOS main App is sole sync DB
writer, publishes atomic App Group snapshots; keyboard reads snapshot and requests existing
confirmed context via gateway when permitted. Keyboard never flushes outbox.
