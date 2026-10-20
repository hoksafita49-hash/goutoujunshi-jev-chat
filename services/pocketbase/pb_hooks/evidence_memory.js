// Domain rules; helpers are supplied by cloud.js to avoid circular modules.
function preferences(data, api) {
  if (!data || typeof data !== "object" || Array.isArray(data)) api.fail("invalid_preferences");
  for (const name of ["tone", "length", "style"]) {
    if (Object.prototype.hasOwnProperty.call(data, name) &&
        (typeof data[name] !== "string" || data[name].length > (name === "style" ? 4000 : 200)))
      api.fail("invalid_preferences");
  }
  if (Object.prototype.hasOwnProperty.call(data, "count") &&
      (!Number.isInteger(data.count) || data.count < 1 || data.count > 3)) api.fail("invalid_preferences");
  return data;
}

function memory(app, owner, data, old, api) {
  if (typeof data.text !== "string" || !data.text.trim() || data.text.length > 4000 ||
      (data.change_reason !== undefined && (typeof data.change_reason !== "string" || data.change_reason.length > 500)))
    api.fail("invalid_memory");
  const sources = data.source_uids === undefined ? [] : data.source_uids;
  if (!Array.isArray(sources) || sources.length > 16 ||
      sources.some((id) => typeof id !== "string" || !/^[0-9a-f-]{36}$/i.test(id)) ||
      new Set(sources).size !== sources.length) api.fail("invalid_memory");
  data.source_uids = sources;
  data.source_type = sources.length ? "messages" : "user";
  data.source_refs = sources.map((id) => {
    const record = api.entity(app, owner, "messages", id);
    if (!record || record.getString("deleted_at")) api.fail("memory_source_unavailable");
    const source = api.parsed(record, "payload");
    if (source.profile_uid !== data.profile_uid || source.confirmed !== true) api.fail("memory_source_mismatch");
    return { message_uid: id, version: record.getInt("version") };
  });
  data.change_reason = (data.change_reason || "").trim() || (old ? "修改记忆" : "创建记忆");
  if (old) {
    const previous = api.parsed(old, "payload");
    if (previous.text !== data.text || previous.kind !== data.kind ||
        api.canonical(previous.source_uids || []) !== api.canonical(sources)) data.status = "pending";
  }
  return data;
}

function history(app, session, data, api) {
  const record = api.entity(app, session.owner, "memories", data.id);
  if (!record || record.getString("deleted_at")) api.fail("not_found", 404);
  const before = data.before_version === undefined ? 0 : data.before_version;
  const limit = data.limit === undefined ? 50 : data.limit;
  if (!Number.isInteger(before) || before < 0 || !Number.isInteger(limit) || limit < 1 || limit > 50)
    api.fail("invalid_request", 422);
  const rows = app.findRecordsByFilter("cloud_changes",
    "owner={:o} && entity_type='memories' && uid={:u} && version<{:v}", "-version", limit + 1, 0,
    {o: session.owner, u: data.id, v: before || record.getInt("version") + 1});
  const versions = rows.slice(0, limit).map((r) => api.parsed(r, "record"));
  return {memory_uid: data.id, profile_uid: api.parsed(record, "payload").profile_uid,
    versions: versions, has_more: rows.length > limit,
    next_before_version: versions.length ? versions[versions.length - 1].version : 0};
}

function evidence(app, session, data, api) {
  const job = api.first(app, "cloud_jobs", "owner={:o} && uid={:u}", {o: session.owner, u: data.id});
  if (!job || job.getString("status") === "cancelled") api.fail("not_found", 404);
  const input = api.parsed(job, "input") || {};
  const conversation = api.entity(app, session.owner, "conversations", job.getString("conversation_uid"));
  if (!conversation || conversation.getString("deleted_at")) api.fail("not_found", 404);
  const messages = (input.messages || []).filter((m) => {
    const live = api.entity(app, session.owner, "messages", m.uid);
    return live && !live.getString("deleted_at") &&
      api.parsed(live, "payload").profile_uid === input.profile_uid;
  });
  const result = api.parsed(job, "result") || {};
  return {analysis_id: job.getString("uid"), conversation_uid: job.getString("conversation_uid"),
    profile_uid: input.profile_uid, context_revision: job.getInt("context_revision"),
    evidence: result.evidence || [], messages: messages};
}

function sourceChanged(app, owner, messageId, api) {
  for (const record of api.all(app, "cloud_entities", "owner={:o} && entity_type='memories' && deleted_at=''", {o: owner})) {
    const data = api.parsed(record, "payload");
    if ((data.source_uids || []).indexOf(messageId) < 0) continue;
    const previous = JSON.parse(JSON.stringify(data));
    data.status = "pending";
    data.change_reason = "来源消息已修改，请重新核对";
    record.set("payload", data);
    record.set("version", record.getInt("version") + 1);
    record.set("updated_at", api.now());
    app.save(record);
    api.emit(app, record);
    memoryChanged(app, owner, previous, data, api);
  }
}

function memoryChanged(app, owner, previous, current, api) {
  const signature = (value) => value && value.status === "confirmed" ? api.canonical({
    profile_uid: value.profile_uid, text: value.text, kind: value.kind,
    source_uids: value.source_uids || [], source_refs: value.source_refs || []}) : null;
  if (signature(previous) === signature(current)) return;
  const profile = (current || previous).profile_uid;
  for (const conversation of api.all(app, "cloud_entities", "owner={:o} && entity_type='conversations' && deleted_at=''", {o: owner})) {
    if (api.parsed(conversation, "payload").profile_uid === profile)
      api.touchConversation(app, owner, conversation.getString("uid"));
  }
}

function deleted(app, record, api) {
  const type = record.getString("entity_type"), owner = record.getString("owner"), id = record.getString("uid");
  if (["messages", "memories", "analyses"].indexOf(type) < 0) return;
  const old = api.parsed(record, "payload") || {};
  const oldMemories = type === "memories" ? [old].concat(api.all(app, "cloud_changes",
    "owner={:o} && entity_type='memories' && uid={:u}", {o: owner, u: id})
    .map((r) => api.parsed(r, "record").payload || {})) : [];
  if (type === "memories") memoryChanged(app, owner, old, null, api);
  if (type === "messages") {
    // Inspect historical sources too: an edit cannot hide a formerly attached source
    // and make deleted content recoverable from memory history.
    for (const memory of api.all(app, "cloud_entities", "owner={:o} && entity_type='memories' && deleted_at=''", {o: owner})) {
      const versions = api.all(app, "cloud_changes", "owner={:o} && entity_type='memories' && uid={:u}",
        {o: owner, u: memory.getString("uid")});
      const linked = versions.some((v) => ((api.parsed(v, "record").payload || {}).source_uids || []).indexOf(id) >= 0);
      if (linked || (api.parsed(memory, "payload").source_uids || []).indexOf(id) >= 0) api.tombstone(app, memory);
    }
  }
  for (const job of api.all(app, "cloud_jobs", "owner={:o}", {o: owner})) {
    const input = api.parsed(job, "input") || {};
    let referenced;
    if (type === "messages") referenced = (input.messages || []).some((m) => m.uid === id);
    else if (type === "memories") referenced = (input.memories || []).some((m) => m.uid === id ||
      (!m.uid && oldMemories.some((v) => v.profile_uid === m.profile_uid && v.text === m.text)));
    else referenced = input.parent_analysis_id === id || (input.parent_result &&
      (input.parent_result.analysis_id === id || (old.result && api.canonical(input.parent_result) === api.canonical(old.result))));
    if (!referenced) continue;
    const analysis = api.entity(app, owner, "analyses", job.getString("uid"));
    if (analysis && !analysis.getString("deleted_at")) api.tombstone(app, analysis);
    else {
      job.set("input", {}); job.set("result", null); job.set("status", "cancelled");
      job.set("error_code", "context_deleted"); app.save(job);
    }
  }
}

module.exports = {preferences: preferences, memory: memory, history: history,
  evidence: evidence, sourceChanged: sourceChanged, memoryChanged: memoryChanged, deleted: deleted};
