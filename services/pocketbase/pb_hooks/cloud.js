// All business mutations and change/receipt writes happen in one PB transaction.
const TYPES = [
  "profiles",
  "conversations",
  "messages",
  "notes",
  "memories",
  "preferences",
  "identity_links",
  "feedback",
  "analyses",
];
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const evidenceMemory = require(__hooks + "/evidence_memory.js");
function domainHelpers() {
  return {entity: entity, parsed: parsed, fail: fail, canonical: canonical, first: first,
    all: all, now: now, emit: emit, tombstone: tombstone, touchConversation: touchConversation};
}
function fail(code, status) {
  const error = new Error(code);
  error.cloudCode = code;
  error.cloudStatus = status || 400;
  throw error;
}
function now() {
  return new Date().toISOString();
}
function uuid() {
  const h = $security.randomStringWithAlphabet(32, "0123456789abcdef");
  return (
    h.slice(0, 8) +
    "-" +
    h.slice(8, 12) +
    "-" +
    h.slice(12, 16) +
    "-" +
    h.slice(16, 20) +
    "-" +
    h.slice(20)
  );
}
function parsed(record, field) {
  return JSON.parse(record.getString(field) || "null");
}
function canonical(value) {
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (value && typeof value === "object")
    return (
      "{" +
      Object.keys(value)
        .sort()
        .map((k) => JSON.stringify(k) + ":" + canonical(value[k]))
        .join(",") +
      "}"
    );
  return JSON.stringify(value);
}
function first(app, collection, filter, params) {
  const records = app.findRecordsByFilter(
    collection,
    filter,
    "",
    1,
    0,
    params || {},
  );
  return records.length ? records[0] : null;
}
function all(app, collection, filter, params) {
  let out = [],
    offset = 0;
  while (true) {
    const page = app.findRecordsByFilter(
      collection,
      filter,
      "id",
      200,
      offset,
      params || {},
    );
    out = out.concat(page);
    if (page.length < 200) return out;
    offset += 200;
  }
}
function meta(app) {
  return app.findFirstRecordByData("cloud_meta", "name", "primary");
}
function entity(app, owner, type, id) {
  return first(
    app,
    "cloud_entities",
    "owner={:o} && entity_type={:t} && uid={:u}",
    { o: owner, t: type, u: id },
  );
}
function publicRecord(record) {
  return {
    entity_type: record.getString("entity_type"),
    uid: record.getString("uid"),
    version: record.getInt("version"),
    created_at: record.getString("created_at"),
    updated_at: record.getString("updated_at"),
    deleted_at: record.getString("deleted_at") || null,
    payload: parsed(record, "payload") || {},
  };
}
function emit(app, record) {
  const state = meta(app);
  const seq = state.getInt("head") + 1;
  state.set("head", seq);
  app.save(state);
  const change = new Record(app.findCollectionByNameOrId("cloud_changes"));
  change.set("owner", record.getString("owner"));
  change.set("entity_type", record.getString("entity_type"));
  change.set("uid", record.getString("uid"));
  change.set("version", record.getInt("version"));
  change.set("seq", seq);
  change.set("record", publicRecord(record));
  app.save(change);
  return seq;
}
function auth(app, e) {
  try {
    const user = app.findAuthRecordByToken(
      e.request.header.get("X-Account-Token"),
      "auth",
    );
    if (user.collection().name !== "cloud_users") fail("unauthorized", 401);
    const device = app.findRecordById(
      "cloud_devices",
      e.request.header.get("X-Device-Session"),
    );
    if (
      device.getString("owner") !== user.id ||
      device.getString("revoked_at") ||
      device.getString("expires_at") <= now() ||
      !$security.equal(
        device.getString("secret_hash"),
        $security.sha256(e.request.header.get("X-Device-Secret")),
      )
    )
      fail("unauthorized", 401);
    return { user: user, device: device, owner: user.id };
  } catch (error) {
    fail("unauthorized", 401);
  }
}
function live(app, owner, type, id) {
  const record = entity(app, owner, type, id);
  if (!record || record.getString("deleted_at")) fail("parent_unavailable");
  return record;
}
function validatePayload(app, owner, type, data, old) {
  if (
    !data ||
    typeof data !== "object" ||
    Array.isArray(data) ||
    Object.prototype.hasOwnProperty.call(data, "owner")
  )
    fail("invalid_payload");
  if (JSON.stringify(data).length > 131072) fail("payload_too_large");
  if (old && type !== "identity_links") {
    const previous = parsed(old, "payload");
    for (const field of ["profile_uid", "conversation_uid"]) {
      if (previous[field] && previous[field] !== data[field])
        fail("parent_immutable");
    }
  }
  if (
    type === "profiles" &&
    (typeof data.name !== "string" ||
      !data.name.trim() ||
      data.name.length > 200)
  )
    fail("invalid_profile");
  if (["conversations", "memories", "identity_links"].indexOf(type) >= 0)
    live(app, owner, "profiles", data.profile_uid);
  if (type === "conversations")
    data.context_revision = old
      ? parsed(old, "payload").context_revision || 0
      : 0;
  if (type === "messages") {
    const conversation = live(
      app,
      owner,
      "conversations",
      data.conversation_uid,
    );
    const p = parsed(conversation, "payload");
    if (p.profile_uid !== data.profile_uid) fail("parent_mismatch");
    if (
      data.confirmed !== true ||
      ["me", "other", "unknown"].indexOf(data.role) < 0 ||
      typeof data.text !== "string" ||
      !data.text.trim() ||
      data.text.length > 32768
    )
      fail("unconfirmed_or_invalid_message");
  }
  if (
    type === "memories" &&
    (["fact", "hypothesis"].indexOf(data.kind) < 0 ||
      ["pending", "confirmed"].indexOf(data.status) < 0 ||
      (data.kind === "hypothesis" && data.status === "confirmed"))
  )
    fail("invalid_memory_status");
  if (type === "feedback") live(app, owner, "analyses", data.analysis_uid);
  if (type === "memories") data = evidenceMemory.memory(app, owner, data, old, domainHelpers());
  if (type === "preferences") {
    evidenceMemory.preferences(data, domainHelpers());
    data = Object.assign({}, old ? parsed(old, "payload") : {}, data);
  }
  return data;
}
function touchConversation(app, owner, id) {
  const record = live(app, owner, "conversations", id);
  const data = parsed(record, "payload");
  data.context_revision = (data.context_revision || 0) + 1;
  record.set("payload", data);
  record.set("version", record.getInt("version") + 1);
  record.set("updated_at", now());
  app.save(record);
  emit(app, record);
}
function tombstone(app, record) {
  record = app.findRecordById("cloud_entities", record.id);
  if (record.getString("deleted_at")) return;
  const owner = record.getString("owner"),
    type = record.getString("entity_type"),
    id = record.getString("uid");
  evidenceMemory.deleted(app, record, domainHelpers());
  const children =
    ["profiles", "conversations", "analyses"].indexOf(type) >= 0
      ? all(app, "cloud_entities", "owner={:o} && deleted_at=''", { o: owner })
      : [];
  for (const child of children) {
    const p = parsed(child, "payload") || {};
    if (
      (type === "profiles" && p.profile_uid === id) ||
      (type === "conversations" && p.conversation_uid === id) ||
      (type === "analyses" && p.analysis_uid === id)
    )
      tombstone(app, child);
  }
  // Cascades may advance this conversation while invalidating confirmed memory.
  record = app.findRecordById("cloud_entities", record.id);
  const stamp = now();
  record.set("deleted_at", stamp);
  record.set("updated_at", stamp);
  record.set("payload", {});
  record.set("version", record.getInt("version") + 1);
  app.save(record);
  for (const change of all(
    app,
    "cloud_changes",
    "owner={:o} && entity_type={:t} && uid={:u}",
    { o: owner, t: type, u: id },
  )) {
    const value = parsed(change, "record");
    value.payload = {};
    value.deleted_at = stamp;
    change.set("record", value);
    app.save(change);
  }
  if (type === "conversations" || type === "analyses") {
    for (const job of all(app, "cloud_jobs", "owner={:o}", { o: owner })) {
      if (
        (type === "conversations" &&
          job.getString("conversation_uid") === id) ||
        (type === "analyses" && job.getString("uid") === id)
      ) {
        job.set("input", {});
        job.set("result", null);
        job.set("status", "cancelled");
        job.set("error_code", "context_deleted");
        app.save(job);
      }
    }
  }
  emit(app, record);
}
function receiptResult(app, receipt) {
  const value = parsed(receipt, "result");
  if (value.record) {
    const ref = value.record;
    const change = first(
      app,
      "cloud_changes",
      "owner={:o} && entity_type={:t} && uid={:u} && version={:v}",
      {
        o: receipt.getString("owner"),
        t: ref.entity_type,
        u: ref.uid,
        v: ref.version,
      },
    );
    value.record = change ? parsed(change, "record") : ref;
  }
  return value;
}
function apply(app, session, operation) {
  const result = { op_id: operation.op_id, status: "rejected" };
  try {
    if (
      !UUID.test(operation.op_id) ||
      !UUID.test(operation.entity_uid) ||
      TYPES.indexOf(operation.entity_type) < 0 ||
      operation.entity_type === "analyses" ||
      ["upsert", "delete"].indexOf(operation.action) < 0 ||
      !Number.isInteger(operation.base_version) ||
      operation.base_version < 0 ||
      Object.keys(operation).some(
        (k) =>
          [
            "op_id",
            "entity_type",
            "entity_uid",
            "base_version",
            "action",
            "payload",
          ].indexOf(k) < 0,
      )
    )
      fail("invalid_operation");
    const digest = $security.sha256(canonical(operation));
    const prior = first(
      app,
      "cloud_receipts",
      "owner={:o} && device_id={:d} && op_id={:i}",
      { o: session.owner, d: session.device.id, i: operation.op_id },
    );
    if (prior) {
      if (!$security.equal(prior.getString("digest"), digest))
        return {
          op_id: operation.op_id,
          status: "rejected",
          code: "op_id_reused",
        };
      return receiptResult(app, prior);
    }
    let record = entity(
      app,
      session.owner,
      operation.entity_type,
      operation.entity_uid,
    );
    if (record && record.getString("deleted_at")) fail("entity_deleted");
    if ((record ? record.getInt("version") : 0) !== operation.base_version) {
      result.status = "conflict";
      if (record) result.record = publicRecord(record);
      result.code = "version_conflict";
    } else {
      if (operation.action === "delete") {
        if (!record) fail("entity_missing");
        const previous = parsed(record, "payload");
        tombstone(app, record);
        record = app.findRecordById("cloud_entities", record.id);
        if (operation.entity_type === "messages")
          touchConversation(app, session.owner, previous.conversation_uid);
      } else {
        const previousPayload = record ? parsed(record, "payload") : null;
        const payload = validatePayload(
          app,
          session.owner,
          operation.entity_type,
          JSON.parse(JSON.stringify(operation.payload || {})),
          record,
        );
        if (!record) {
          record = new Record(app.findCollectionByNameOrId("cloud_entities"));
          record.set("owner", session.owner);
          record.set("entity_type", operation.entity_type);
          record.set("uid", operation.entity_uid);
          record.set("created_at", now());
        }
        record.set("version", operation.base_version + 1);
        record.set("updated_at", now());
        record.set("payload", payload);
        app.save(record);
        emit(app, record);
        if (operation.entity_type === "messages")
          touchConversation(app, session.owner, payload.conversation_uid);
        if (operation.entity_type === "messages" && previousPayload && canonical(previousPayload) !== canonical(payload))
          evidenceMemory.sourceChanged(app, session.owner, operation.entity_uid, domainHelpers());
        if (operation.entity_type === "memories")
          evidenceMemory.memoryChanged(app, session.owner, previousPayload, payload, domainHelpers());
      }
      result.status = "applied";
      result.record = publicRecord(record);
      result.seq = meta(app).getInt("head");
    }
    const saved = JSON.parse(JSON.stringify(result));
    if (saved.record)
      saved.record = {
        entity_type: saved.record.entity_type,
        uid: saved.record.uid,
        version: saved.record.version,
      };
    const receipt = new Record(app.findCollectionByNameOrId("cloud_receipts"));
    receipt.set("owner", session.owner);
    receipt.set("device_id", session.device.id);
    receipt.set("op_id", operation.op_id);
    receipt.set("digest", digest);
    receipt.set("result", saved);
    app.save(receipt);
    return result;
  } catch (error) {
    throw error;
  }
}
function pull(app, session, data) {
  const state = meta(app),
    epoch = state.getString("epoch");
  let position = 0,
    bound = state.getInt("head");
  if (data.cursor) {
    let cursor;
    try {
      cursor = $security.parseJWT(
        data.cursor,
        $os.getenv("GOUTOU_PB_INTERNAL_KEY"),
      );
    } catch (error) {
      fail("invalid_cursor", 409);
    }
    if (cursor.owner !== session.owner) fail("invalid_cursor", 403);
    if (cursor.epoch !== epoch) fail("server_epoch_changed", 409);
    position = cursor.after;
    if (cursor.after < cursor.bound) bound = cursor.bound;
  }
  const limit = Math.min(200, Math.max(1, Number(data.limit) || 200));
  const rows = app.findRecordsByFilter(
    "cloud_changes",
    "owner={:o} && seq>{:p} && seq<={:b}",
    "seq",
    limit + 1,
    0,
    { o: session.owner, p: position, b: bound },
  );
  const more = rows.length > limit;
  const page = rows.slice(0, limit);
  const after = more ? page[page.length - 1].getInt("seq") : bound;
  const cursor = $security.createJWT(
    { owner: session.owner, epoch: epoch, after: after, bound: bound },
    $os.getenv("GOUTOU_PB_INTERNAL_KEY"),
    86400 * 365,
  );
  return {
    server_epoch: epoch,
    changes: page.map((r) => ({
      seq: r.getInt("seq"),
      record: parsed(r, "record"),
    })),
    cursor: cursor,
    has_more: more,
  };
}
function jobView(job) {
  return {
    analysis_id: job.getString("uid"),
    status: job.getString("status"),
    conversation_uid: job.getString("conversation_uid"),
    context_revision: job.getInt("context_revision"),
    result: parsed(job, "result"),
    error: job.getString("error_code")
      ? { code: job.getString("error_code") }
      : null,
  };
}
function publishJob(app, job) {
  const view = jobView(job),
    input = parsed(job, "input") || {};
  view.profile_uid = input.profile_uid;
  let record = entity(
    app,
    job.getString("owner"),
    "analyses",
    job.getString("uid"),
  );
  if (!record) {
    record = new Record(app.findCollectionByNameOrId("cloud_entities"));
    record.set("owner", job.getString("owner"));
    record.set("entity_type", "analyses");
    record.set("uid", job.getString("uid"));
    record.set("created_at", now());
  }
  record.set("version", record.getInt("version") + 1);
  record.set("updated_at", now());
  record.set("payload", view);
  app.save(record);
  emit(app, record);
}
function createJob(app, session, data) {
  if (
    !UUID.test(data.request_id) ||
    !UUID.test(data.conversation_uid) ||
    !Number.isInteger(data.context_revision) ||
    ["analysis", "explanation", "rewrite"].indexOf(data.kind) < 0 ||
    Object.keys(data).some(
      (k) =>
        [
          "request_id",
          "conversation_uid",
          "context_revision",
          "kind",
          "parent_analysis_id",
          "selected_text",
          "preferences",
        ].indexOf(k) < 0,
    )
  )
    fail("invalid_analysis");
  const digest = $security.sha256(canonical(data));
  const prior = first(app, "cloud_jobs", "owner={:o} && request_id={:r}", {
    o: session.owner,
    r: data.request_id,
  });
  if (prior) {
    if (!$security.equal(prior.getString("digest"), digest))
      fail("request_id_reused", 409);
    const view = jobView(prior);
    return {
      analysis_id: view.analysis_id,
      status: view.status,
      conversation_uid: view.conversation_uid,
      context_revision: view.context_revision,
    };
  }
  const conversation = live(
      app,
      session.owner,
      "conversations",
      data.conversation_uid,
    ),
    context = parsed(conversation, "payload");
  if ((context.context_revision || 0) !== data.context_revision)
    fail("context_revision_conflict", 409);
  const profile = live(app, session.owner, "profiles", context.profile_uid);
  const messages = all(
    app,
    "cloud_entities",
    "owner={:o} && entity_type='messages' && deleted_at=''",
    { o: session.owner },
  )
    .map((r) => {
      const p = parsed(r, "payload");
      p.uid = r.getString("uid");
      p.version = r.getInt("version");
      return p;
    })
    .filter(
      (p) =>
        p.conversation_uid === data.conversation_uid && p.confirmed === true,
    );
  messages.sort(
    (a, b) =>
      String(a.order_key || a.observed_at || a.uid).localeCompare(
        String(b.order_key || b.observed_at || b.uid),
      ) || String(a.uid).localeCompare(String(b.uid)),
  );
  if (!messages.length) fail("confirmed_context_required");
  const memories = all(
    app,
    "cloud_entities",
    "owner={:o} && entity_type='memories' && deleted_at=''",
    { o: session.owner },
  )
    .map((r) => Object.assign({}, parsed(r, "payload"), {uid: r.getString("uid")}))
    .filter(
      (p) => p.profile_uid === context.profile_uid && p.status === "confirmed",
    );
  const notes = all(
    app,
    "cloud_entities",
    "owner={:o} && entity_type='notes' && deleted_at=''",
    { o: session.owner },
  )
    .map((r) => parsed(r, "payload"))
    .filter((p) => p.enabled !== false);
  const prefs = entity(
    app,
    session.owner,
    "preferences",
    "00000000-0000-4000-8000-000000000001",
  );
  const input = {
    profile_uid: context.profile_uid,
    profile: parsed(profile, "payload"),
    messages: messages.slice(-64),
    memories: memories.slice(-30),
    notes: notes.slice(0, 30),
    preferences: Object.assign({}, prefs && !prefs.getString("deleted_at") ? parsed(prefs, "payload") : {},
      data.preferences === undefined ? {} : evidenceMemory.preferences(data.preferences, domainHelpers())),
    selected_text: data.selected_text || "",
  };
  if (
    String(input.selected_text).length > 4096 ||
    JSON.stringify(input).length > 131072
  )
    fail("context_too_large");
  if (data.kind !== "analysis") {
    const parent = first(app, "cloud_jobs", "owner={:o} && uid={:u}", {
      o: session.owner,
      u: data.parent_analysis_id || "",
    });
    if (
      !parent ||
      parent.getString("conversation_uid") !== data.conversation_uid ||
      parent.getString("status") !== "succeeded"
    )
      fail("parent_analysis_unavailable");
    input.parent_result = parsed(parent, "result");
    input.parent_analysis_id = parent.getString("uid");
  }
  const job = new Record(app.findCollectionByNameOrId("cloud_jobs"));
  job.set("owner", session.owner);
  job.set("uid", uuid());
  job.set("request_id", data.request_id);
  job.set("digest", digest);
  job.set("kind", data.kind);
  job.set("status", "queued");
  job.set("conversation_uid", data.conversation_uid);
  job.set("context_revision", data.context_revision);
  job.set("input", input);
  job.set("created_at", now());
  job.set("updated_at", now());
  app.save(job);
  publishJob(app, job);
  return {
    analysis_id: job.getString("uid"),
    status: "queued",
    conversation_uid: data.conversation_uid,
    context_revision: data.context_revision,
  };
}
function workerAction(app, action, data) {
  let output;
  app.runInTransaction((tx) => {
    if (action === "job-claim") {
      const token = String(data.claim_token || "");
      if (!/^[A-Za-z0-9_-]{64,128}$/.test(token)) fail("invalid_claim_token");
      const digest = $security.sha256(token);
      const prior = first(tx, "cloud_jobs", "claim_token={:t}", { t: digest });
      if (prior) {
        output =
          prior.getString("status") === "running"
            ? claimedJobView(prior, token)
            : { job: null };
        return;
      }
      const queued = tx.findRecordsByFilter(
        "cloud_jobs",
        "status='queued'",
        "created_at",
        1,
        0,
      );
      if (!queued.length) {
        output = { job: null };
        return;
      }
      const job = queued[0];
      job.set("status", "running");
      job.set("claim_token", digest);
      job.set("updated_at", now());
      tx.save(job);
      publishJob(tx, job);
      output = claimedJobView(job, token);
      return;
    }
    if (action === "job-check") {
      const job = first(tx, "cloud_jobs", "uid={:u}", { u: data.analysis_id });
      if (
        !job ||
        job.getString("status") !== "running" ||
        !$security.equal(
          job.getString("claim_token"),
          $security.sha256(String(data.claim_token || "")),
        )
      )
        fail("claim_lost", 409);
      output = { status: "running" };
      return;
    }
    if (action === "job-finish") {
      const job = first(tx, "cloud_jobs", "uid={:u}", { u: data.analysis_id });
      if (
        !job ||
        !$security.equal(
          job.getString("claim_token"),
          $security.sha256(String(data.claim_token || "")),
        )
      )
        fail("claim_lost", 409);
      if (["succeeded", "failed", "indeterminate"].indexOf(data.status) < 0)
        fail("invalid_job_status");
      if (job.getString("status") !== "running") {
        if (
          job.getString("status") === data.status &&
          canonical(parsed(job, "result")) ===
            canonical(data.status === "succeeded" ? data.result : null) &&
          job.getString("error_code") === (data.error_code || "")
        ) {
          output = jobView(job);
          return;
        }
        fail("claim_lost", 409);
      }
      job.set("status", data.status);
      job.set("result", data.status === "succeeded" ? data.result : null);
      job.set("error_code", data.error_code || "");
      job.set("updated_at", now());
      tx.save(job);
      publishJob(tx, job);
      output = jobView(job);
      return;
    }
    if (action === "job-recover") {
      const running = all(tx, "cloud_jobs", "status='running'", {});
      for (const job of running) {
        job.set("status", "indeterminate");
        job.set("error_code", "gateway_restarted_outcome_unknown");
        job.set("claim_token", "");
        job.set("updated_at", now());
        tx.save(job);
        publishJob(tx, job);
      }
      output = { recovered: running.length };
      return;
    }
    if (action === "reset-epoch") {
      const state = meta(tx);
      state.set("epoch", uuid());
      tx.save(state);
      for (const device of all(tx, "cloud_devices", "revoked_at=''", {})) {
        device.set("revoked_at", now());
        tx.save(device);
      }
      for (const job of all(
        tx,
        "cloud_jobs",
        "status='queued' || status='running'",
        {},
      )) {
        job.set("status", "indeterminate");
        job.set("error_code", "database_restored");
        job.set("claim_token", "");
        tx.save(job);
        publishJob(tx, job);
      }
      output = { server_epoch: state.getString("epoch") };
      return;
    }
  });
  return output;
}
function claimedJobView(job, token) {
  return {
    job: Object.assign(jobView(job), {
      owner: job.getString("owner"),
      kind: job.getString("kind"),
      input: parsed(job, "input"),
    }),
    claim_token: token,
  };
}
function dispatch(e) {
  try {
    const secret = $os.getenv("GOUTOU_PB_INTERNAL_KEY");
    if (
      secret.length < 40 ||
      !$security.equal(
        $security.sha256(secret),
        $security.sha256(e.request.header.get("X-Internal-Key")),
      )
    )
      fail("forbidden", 403);
    const action = e.request.pathValue("action"),
      data = e.requestInfo().body || {},
      app = e.app;
    if (
      [
        "job-claim",
        "job-check",
        "job-finish",
        "job-recover",
        "reset-epoch",
      ].indexOf(action) >= 0
    )
      return e.json(200, workerAction(app, action, data));
    if (action === "provision") {
      if (
        typeof data.email !== "string" ||
        typeof data.password !== "string" ||
        data.password.length < 12
      )
        fail("invalid_credentials");
      const email = data.email.trim().toLowerCase();
      let existing;
      try {
        existing = app.findAuthRecordByEmail("cloud_users", email);
      } catch (error) {
        existing = null;
      }
      if (existing) {
        if (!existing.validatePassword(data.password))
          fail("account_exists", 409);
        return e.json(200, {
          id: existing.id,
          email: existing.getString("email"),
        });
      }
      const user = new Record(app.findCollectionByNameOrId("cloud_users"));
      user.set("email", email);
      user.setPassword(data.password);
      app.save(user);
      return e.json(200, { id: user.id, email: user.getString("email") });
    }
    if (action === "login") {
      let user;
      try {
        user = app.findAuthRecordByEmail(
          "cloud_users",
          String(data.email || "")
            .trim()
            .toLowerCase(),
        );
        if (!user.validatePassword(String(data.password || "")))
          fail("unauthorized", 401);
      } catch (error) {
        fail("unauthorized", 401);
      }
      if (
        ["windows", "android", "ios"].indexOf(data.platform) < 0 ||
        typeof data.device_name !== "string" ||
        !data.device_name.trim() ||
        data.device_name.length > 100
      )
        fail("invalid_device");
      const token = $security.randomString(64);
      const device = new Record(app.findCollectionByNameOrId("cloud_devices"));
      device.set("owner", user.id);
      device.set("name", data.device_name);
      device.set("platform", data.platform);
      device.set("secret_hash", $security.sha256(token));
      device.set(
        "expires_at",
        new Date(Date.now() + 30 * 86400000).toISOString(),
      );
      app.save(device);
      return e.json(200, {
        api_version: 1,
        token: user.newAuthToken(),
        user: { id: user.id, email: user.getString("email") },
        device: {
          id: device.id,
          name: data.device_name,
          expires_at: device.getString("expires_at"),
        },
        device_secret: token,
        server_epoch: meta(app).getString("epoch"),
      });
    }
    const session = auth(app, e);
    if (action === "job-create") {
      let result;
      app.runInTransaction((tx) => {
        const current = auth(tx, e);
        result = createJob(tx, current, data);
      });
      return e.json(202, result);
    }
    if (action === "job-get") {
      const job = first(app, "cloud_jobs", "owner={:o} && uid={:u}", {
        o: session.owner,
        u: data.id,
      });
      if (!job) fail("not_found", 404);
      return e.json(200, jobView(job));
    }
    if (action === "memory-history" || action === "job-evidence") {
      let result;
      app.runInTransaction((tx) => {
        const current = auth(tx, e);
        result = action === "memory-history" ? evidenceMemory.history(tx, current, data, domainHelpers()) :
          evidenceMemory.evidence(tx, current, data, domainHelpers());
      });
      return e.json(200, result);
    }
    if (action === "auth")
      return e.json(200, {
        owner: session.owner,
        server_epoch: meta(app).getString("epoch"),
        device_id: session.device.id,
      });
    if (action === "refresh")
      return e.json(200, { token: session.user.newAuthToken() });
    if (action === "logout" || action === "revoke") {
      const device =
        action === "logout"
          ? session.device
          : app.findRecordById("cloud_devices", String(data.id));
      if (device.getString("owner") !== session.owner) fail("not_found", 404);
      device.set("revoked_at", now());
      app.save(device);
      return e.json(200, { status: "revoked" });
    }
    if (action === "devices")
      return e.json(200, {
        devices: all(app, "cloud_devices", "owner={:o}", {
          o: session.owner,
        }).map((d) => ({
          id: d.id,
          name: d.getString("name"),
          platform: d.getString("platform"),
          expires_at: d.getString("expires_at"),
          revoked_at: d.getString("revoked_at") || null,
        })),
      });
    if (action === "pull") return e.json(200, pull(app, session, data));
    if (action === "push") {
      if (data.server_epoch !== meta(app).getString("epoch"))
        fail("server_epoch_changed", 409);
      if (!Array.isArray(data.operations) || data.operations.length > 50)
        fail("invalid_batch");
      const results = [],
        failed = {};
      for (const operation of data.operations) {
        const target = operation.entity_type + ":" + operation.entity_uid,
          payload = operation.payload || {};
        const dependent =
          failed[target] ||
          failed["profiles:" + payload.profile_uid] ||
          failed["conversations:" + payload.conversation_uid] ||
          failed["analyses:" + payload.analysis_uid];
        let result;
        if (dependent)
          result = {
            op_id: operation.op_id,
            status: "rejected",
            code: "dependency_failed",
          };
        else {
          try {
            app.runInTransaction((tx) => {
              const current = auth(tx, e);
              result = apply(tx, current, operation);
            });
          } catch (error) {
            if (!error.cloudCode || error.cloudStatus === 401) throw error;
            result = {
              op_id: operation.op_id,
              status: "rejected",
              code: error.cloudCode,
            };
          }
        }
        if (result.status !== "applied") failed[target] = true;
        results.push(result);
      }
      return e.json(200, {
        server_epoch: meta(app).getString("epoch"),
        results: results,
      });
    }
    fail("not_found", 404);
  } catch (error) {
    return e.json(error.cloudStatus || 500, {
      error: { code: error.cloudCode || "internal_error" },
    });
  }
}
module.exports = {
  dispatch: dispatch,
  entity: entity,
  live: live,
  emit: emit,
  publicRecord: publicRecord,
  meta: meta,
  uuid: uuid,
  now: now,
  parsed: parsed,
  all: all,
  first: first,
  fail: fail,
  canonical: canonical,
  auth: auth,
};
