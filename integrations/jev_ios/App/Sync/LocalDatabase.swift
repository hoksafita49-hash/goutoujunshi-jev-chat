import Foundation
import SQLite3

/// Only the main app target compiles this file. No extension opens the sync DB.
final class CloudDatabase {
    private var db: OpaquePointer?
    let url: URL
    private let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)
    init(url: URL) throws {
        self.url = url
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        guard sqlite3_open_v2(url.path, &db, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX, nil) == SQLITE_OK else { throw CloudError.protocolError }
        sqlite3_busy_timeout(db, 3000)
        try execute("PRAGMA journal_mode=WAL")
        try execute("PRAGMA synchronous=FULL")
        try execute("CREATE TABLE IF NOT EXISTS mirror(kind TEXT, uid TEXT, body TEXT NOT NULL, PRIMARY KEY(kind,uid))")
        try execute("CREATE TABLE IF NOT EXISTS outbox(position INTEGER PRIMARY KEY AUTOINCREMENT, op_id TEXT UNIQUE, body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', remote TEXT, code TEXT)")
        try execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        try execute("CREATE TABLE IF NOT EXISTS legacy_id_map(legacy_id TEXT PRIMARY KEY, uid TEXT NOT NULL)")
        try execute("CREATE TABLE IF NOT EXISTS jobs(request_id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        #if os(iOS)
        try FileManager.default.setAttributes([.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication], ofItemAtPath: url.path)
        #endif
        var file = url, values = URLResourceValues(); values.isExcludedFromBackup = true; try file.setResourceValues(values)
    }
    deinit { sqlite3_close(db) }
    private func execute(_ sql: String, _ values: [String] = []) throws {
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { throw databaseError() }
        defer { sqlite3_finalize(stmt) }
        for (index, value) in values.enumerated() { sqlite3_bind_text(stmt, Int32(index + 1), value, -1, transient) }
        let status = sqlite3_step(stmt)
        guard status == SQLITE_DONE || status == SQLITE_ROW else { throw databaseError() }
    }
    private func rows(_ sql: String, _ values: [String] = []) throws -> [[String]] {
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { throw databaseError() }
        defer { sqlite3_finalize(stmt) }
        for (index, value) in values.enumerated() { sqlite3_bind_text(stmt, Int32(index + 1), value, -1, transient) }
        var result = [[String]]()
        while true {
            let status = sqlite3_step(stmt)
            if status == SQLITE_DONE { return result }
            guard status == SQLITE_ROW else { throw databaseError() }
            result.append((0..<sqlite3_column_count(stmt)).map { index in
                sqlite3_column_text(stmt, index).map { String(cString: $0) } ?? ""
            })
        }
    }
    private func databaseError() -> GoutouError { .invalid("本机同步数据库读写失败（\(sqlite3_errcode(db))）") }
    private func text<T: Encodable>(_ value: T) throws -> String { String(decoding: try CloudJSON.encoder.encode(value), as: UTF8.self) }
    private func decoded<T: Decodable>(_ type: T.Type, _ text: String) throws -> T { try CloudJSON.decoder.decode(type, from: Data(text.utf8)) }
    private func transaction(_ body: () throws -> Void) throws {
        try execute("BEGIN IMMEDIATE")
        do { try body(); try execute("COMMIT") } catch { try? execute("ROLLBACK"); throw error }
    }
    func meta(_ key: String) throws -> String? { try rows("SELECT value FROM metadata WHERE key=?", [key]).first?.first }
    func setMeta(_ key: String, _ value: String) throws { try execute("INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", [key, value]) }
    func cursor() throws -> String { try meta("cursor") ?? "" }
    func recoveryRequired() throws -> Bool { try meta("recovery_required") == "true" }
    private func mirror(_ record: CloudRecord) throws {
        if let row = try rows("SELECT body FROM mirror WHERE kind=? AND uid=?", [record.entityType, record.uid]).first {
            let current = try decoded(CloudRecord.self, row[0])
            if current.version >= record.version { return }
        }
        try execute("INSERT INTO mirror(kind,uid,body) VALUES(?,?,?) ON CONFLICT(kind,uid) DO UPDATE SET body=excluded.body", [record.entityType, record.uid, try text(record)])
    }
    func remoteRecords(_ kind: String) throws -> [CloudRecord] { try rows("SELECT body FROM mirror WHERE kind=? ORDER BY uid", [kind]).map { try decoded(CloudRecord.self, $0[0]) } }
    func operations(includeHeld: Bool = true) throws -> [CloudOperation] {
        try rows("SELECT body FROM outbox" + (includeHeld ? "" : " WHERE state='pending'") + " ORDER BY position").map { try decoded(CloudOperation.self, $0[0]) }
    }
    func visibleRecords(_ kind: String) throws -> [CloudRecord] {
        var records = Dictionary(uniqueKeysWithValues: try remoteRecords(kind).map { ($0.uid, $0) })
        for op in try operations() where op.entityType == kind {
            if records[op.entityUid]?.deletedAt != nil { continue }
            if op.action == "delete" { records.removeValue(forKey: op.entityUid); continue }
            records[op.entityUid] = CloudRecord(entityType: kind, uid: op.entityUid, version: op.baseVersion,
                createdAt: records[op.entityUid]?.createdAt ?? "", updatedAt: "", deletedAt: nil, payload: op.payload)
        }
        return records.values.filter { $0.deletedAt == nil }.sorted { $0.uid < $1.uid }
    }
    private func enqueue(_ operation: CloudOperation) throws { try execute("INSERT INTO outbox(op_id,body,state) VALUES(?,?,'pending')", [operation.opId, try text(operation)]) }
    private func stageInside(entityType: String, uid: String, payload: [String: JSONValue], action: String = "upsert", baseVersion: Int? = nil) throws {
        guard UUID(uuidString: uid) != nil else { throw GoutouError.invalid("云端人物必须使用 UUID；请先预览导入旧资料") }
        let record = try remoteRecords(entityType).first { $0.uid == uid }
        guard record?.deletedAt == nil else { throw GoutouError.invalid("该资料已删除，不能恢复旧记录") }
        // Every queued operation is immutable. Never change a possibly accepted
        // operation's payload under the same idempotency ID.
        try enqueue(.init(entityType: entityType, entityUid: uid, baseVersion: baseVersion ?? record?.version ?? 0, action: action, payload: payload))
    }
    func stage(entityType: String, uid: String, payload: [String: JSONValue], action: String = "upsert", baseVersion: Int? = nil) throws {
        try transaction { try stageInside(entityType: entityType, uid: uid, payload: payload, action: action, baseVersion: baseVersion) }
    }
    func hasPending(entityType: String, uid: String) throws -> Bool {
        try operations().contains { $0.entityType == entityType && $0.entityUid == uid }
    }
    func memories(profileUid: String) throws -> [CloudRecord] {
        try visibleRecords("memories").filter { $0.payload["profile_uid"]?.string == profileUid }
    }
    func saveMemory(uid: String, draft: CloudMemoryDraft, expectedVersion: Int? = nil) throws {
        try transaction {
            guard try visibleRecords("profiles").contains(where: { $0.uid == draft.profileUid }) else { throw GoutouError.invalid("先选择当前账号的人物") }
            let existing = try visibleRecords("memories").first { $0.uid == uid }
            guard existing == nil || existing?.payload["profile_uid"]?.string == draft.profileUid else { throw CloudError.protocolError }
            let messages = try visibleRecords("messages")
            for source in Set(draft.sourceUids) {
                guard messages.contains(where: { $0.uid == source && $0.payload["profile_uid"]?.string == draft.profileUid && $0.payload["confirmed"] == .bool(true) }) else {
                    throw GoutouError.invalid("引用消息已不可用或不属于当前人物；请核对来源。")
                }
            }
            var reviewed = draft
            let previous = existing.map(CloudMemoryDraft.init(record:))
            if previous == nil || previous?.text != draft.text || previous?.kind != draft.kind || Set(previous?.sourceUids ?? []) != Set(draft.sourceUids) { reviewed.status = "pending" }
            // Validate the user's original request before applying the pending rule.
            _ = try draft.payload()
            try stageInside(entityType: "memories", uid: uid, payload: reviewed.payload(preserving: existing?.payload ?? [:]), baseVersion: expectedVersion)
        }
    }
    func restoreMemory(_ revision: CloudRecord) throws {
        guard revision.entityType == "memories", revision.deletedAt == nil,
              let current = try remoteRecords("memories").first(where: { $0.uid == revision.uid && $0.deletedAt == nil }),
              current.payload["profile_uid"]?.string == revision.payload["profile_uid"]?.string else {
            throw GoutouError.invalid("该记忆已删除或不属于当前人物，无法恢复。")
        }
        var draft = CloudMemoryDraft(record: revision); draft.status = "pending"; draft.changeReason = "恢复版本 \(revision.version)"
        try saveMemory(uid: current.uid, draft: draft, expectedVersion: current.version)
    }
    func applyPull(_ page: CloudPullResponse) throws {
        if let epoch = try meta("epoch"), epoch != page.serverEpoch { try preserveRecovery(); throw CloudError.recovery }
        try transaction {
            for change in page.changes { try mirror(change.record) }
            try setMeta("cursor", page.cursor); try setMeta("epoch", page.serverEpoch)
        }
    }
    func applyPush(_ response: CloudPushResponse) throws {
        if let epoch = try meta("epoch"), epoch != response.serverEpoch { try preserveRecovery(); throw CloudError.recovery }
        try transaction {
            for receipt in response.results {
                guard try rows("SELECT op_id FROM outbox WHERE op_id=?", [receipt.opId]).count == 1 else { throw CloudError.protocolError }
                if let record = receipt.record { try mirror(record) }
                if receipt.status == "applied" {
                    try execute("DELETE FROM outbox WHERE op_id=?", [receipt.opId])
                } else {
                    try execute("UPDATE outbox SET state=?,remote=?,code=? WHERE op_id=?", [receipt.status, try receipt.record.map { try text($0) } ?? "", receipt.code ?? receipt.status, receipt.opId])
                }
            }
            try setMeta("epoch", response.serverEpoch)
        }
    }
    func conflicts() throws -> [CloudConflict] {
        try rows("SELECT body,remote,code FROM outbox WHERE state!='pending' ORDER BY position").map {
            CloudConflict(operation: try decoded(CloudOperation.self, $0[0]), remote: $0[1].isEmpty ? nil : try decoded(CloudRecord.self, $0[1]), code: $0[2])
        }
    }
    func resolve(opId: String, useLocal: Bool) throws {
        guard let conflict = try conflicts().first(where: { $0.id == opId }) else { return }
        try transaction {
            if useLocal {
                guard conflict.remote?.deletedAt == nil else { throw GoutouError.invalid("服务端已删除，不能覆盖墓碑；请另建人物") }
                var operation = conflict.operation; operation.opId = UUID().uuidString.lowercased(); operation.baseVersion = conflict.remote?.version ?? operation.baseVersion
                try enqueue(operation)
            }
            try execute("DELETE FROM outbox WHERE op_id=?", [opId])
        }
    }
    func legacyUid(_ id: String) throws -> String? { try rows("SELECT uid FROM legacy_id_map WHERE legacy_id=?", [id]).first?.first }
    func importProfiles(_ profiles: [Relationship]) throws {
        try transaction {
            for profile in profiles where try legacyUid(profile.id) == nil {
                try profile.validate()
                let uid = UUID().uuidString.lowercased()
                try execute("INSERT INTO legacy_id_map(legacy_id,uid) VALUES(?,?)", [profile.id, uid])
                try stageInside(entityType: "profiles", uid: uid, payload: profile.cloudPayload)
            }
        }
    }
    func confirmConversation(profileUid: String, transcript: String, source: String) throws -> String {
        try Transcript(text: transcript, confirmed: true).validate()
        guard try visibleRecords("profiles").contains(where: { $0.uid == profileUid }) else { throw GoutouError.invalid("先选择云端人物") }
        let uid = UUID().uuidString.lowercased(), observed = CloudJSON.now()
        try transaction {
            try stageInside(entityType: "conversations", uid: uid, payload: ["profile_uid": .string(profileUid), "title": .string(source), "platform": .string("ios")])
            for (index, line) in transcript.components(separatedBy: .newlines).filter({ !$0.trimmed.isEmpty }).enumerated() {
                var text = line, role = "unknown"
                for (prefix, speaker) in [("我：", "me"), ("我:", "me"), ("对方：", "other"), ("对方:", "other")] where text.hasPrefix(prefix) {
                    text = String(text.dropFirst(prefix.count)); role = speaker; break
                }
                try stageInside(entityType: "messages", uid: UUID().uuidString.lowercased(), payload: ["conversation_uid": .string(uid), "profile_uid": .string(profileUid),
                    "role": .string(role), "text": .string(text), "confirmed": .bool(true), "observed_at": .string(observed), "order_key": .string(String(format: "%08d", index))])
            }
        }
        return uid
    }
    func contexts(profileUid: String? = nil) throws -> [CloudContext] {
        let live = Set(try visibleRecords("profiles").map(\.uid))
        let messages = try remoteRecords("messages").filter { $0.deletedAt == nil && $0.payload["confirmed"] == .bool(true) }
        let analyses = try remoteRecords("analyses").filter { $0.deletedAt == nil }.sorted { $0.updatedAt < $1.updatedAt }.compactMap { record -> CloudAnalysisResult? in
            let value = record.payload["result"] ?? .object(record.payload)
            return try? CloudJSON.decode(CloudAnalysisResult.self, value: value)
        }
        let dirty = try operations()
        let dirtyConversations = Set(dirty.filter { $0.entityType == "conversations" }.map(\.entityUid) + dirty.filter { $0.entityType == "messages" }.compactMap { $0.payload["conversation_uid"]?.string })
        let dirtyProfiles = Set(dirty.filter { $0.entityType == "profiles" }.map(\.entityUid))
        return try remoteRecords("conversations").filter { record in
            guard let owner = record.payload["profile_uid"]?.string else { return false }
            return record.deletedAt == nil && live.contains(owner) && !dirtyProfiles.contains(owner) && (profileUid == nil || owner == profileUid) && !dirtyConversations.contains(record.uid)
        }.sorted { $0.updatedAt > $1.updatedAt }.map { record in
            let uid = record.uid, revision = record.payload["context_revision"]?.int ?? 0
            let rows = messages.filter { $0.payload["conversation_uid"]?.string == uid }.sorted {
                let a = $0.payload["order_key"]?.string ?? $0.payload["observed_at"]?.string ?? $0.uid
                let b = $1.payload["order_key"]?.string ?? $1.payload["observed_at"]?.string ?? $1.uid
                return a == b ? $0.uid < $1.uid : a < b
            }
            let transcript = rows.map { row in (row.payload["role"]?.string == "me" ? "我：" : row.payload["role"]?.string == "other" ? "对方：" : "待确认：") + (row.payload["text"]?.string ?? "") }.joined(separator: "\n")
            return CloudContext(profileUid: record.payload["profile_uid"]!.string!, conversationUid: uid, contextRevision: revision, transcript: transcript,
                result: analyses.last { $0.matches(conversationUid: uid, revision: revision) })
        }
    }
    func savePending(_ job: CloudPendingJob) throws { try execute("INSERT INTO jobs(request_id,body) VALUES(?,?) ON CONFLICT(request_id) DO UPDATE SET body=excluded.body", [job.requestId, try text(job)]) }
    func pendingJobs() throws -> [CloudPendingJob] { try rows("SELECT body FROM jobs ORDER BY rowid").map { try decoded(CloudPendingJob.self, $0[0]) } }
    func removePending(_ id: String) throws { try execute("DELETE FROM jobs WHERE request_id=?", [id]) }
    func preserveRecovery() throws { try setMeta("recovery_required", "true") }
    func recoveryCopy() throws -> URL {
        let target = url.deletingLastPathComponent().appendingPathComponent("recovery-" + UUID().uuidString + ".sqlite")
        var destination: OpaquePointer?
        guard sqlite3_open(target.path, &destination) == SQLITE_OK else { throw databaseError() }
        defer { sqlite3_close(destination) }
        guard let backup = sqlite3_backup_init(destination, "main", db, "main") else { throw databaseError() }
        let result = sqlite3_backup_step(backup, -1), finish = sqlite3_backup_finish(backup)
        guard result == SQLITE_DONE, finish == SQLITE_OK else { throw databaseError() }
        return target
    }
    func rebuildAfterRecovery(epoch: String) throws {
        _ = try recoveryCopy()
        try transaction {
            for table in ["mirror", "outbox", "jobs", "metadata"] { try execute("DELETE FROM " + table) }
            try setMeta("epoch", epoch)
        }
    }
}
