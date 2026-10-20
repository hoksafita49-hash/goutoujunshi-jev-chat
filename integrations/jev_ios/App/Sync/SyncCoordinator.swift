import Foundation

@MainActor
final class SyncCoordinator {
    private(set) var session: CloudSession?
    private(set) var database: CloudDatabase?
    private(set) var syncing = false
    private(set) var status = "需登录"
    private var failures = 0
    private var nextAttempt = Date.distantPast
    private let directory: URL
    private let credentials: CloudSessionStorage
    private let clientFactory: (String) throws -> GatewayClient
    private var generation = UUID()
    private var activeSync: UUID?
    init(directory: URL, credentials: CloudSessionStorage = .keychain,
         clientFactory: @escaping (String) throws -> GatewayClient = { try GatewayClient(base: $0) }) {
        self.directory = directory; self.credentials = credentials; self.clientFactory = clientFactory
    }
    private func transition() { generation = UUID(); activeSync = nil; syncing = false }
    private func current(_ captured: CloudSession, generation capturedGeneration: UUID) -> Bool {
        generation == capturedGeneration && session?.accountKey == captured.accountKey && session?.deviceId == captured.deviceId
    }
    private func checkCurrent(_ captured: CloudSession, generation: UUID) throws {
        guard current(captured, generation: generation) else { throw CancellationError() }
    }
    private func client(_ session: CloudSession) throws -> GatewayClient {
        let value = try clientFactory(session.baseUrl); value.session = session; return value
    }
    func restore() throws {
        transition(); session = try credentials.read(); database = nil
        if let session = session { try open(session) }
    }
    func requireLogin() throws {
        transition(); try credentials.clear(); session = nil; CloudCache.clear(directory: directory); status = "账号已过期或设备被撤销，请重新登录；本机队列保留。"
    }
    private func open(_ session: CloudSession) throws {
        database = try CloudDatabase(url: directory.appendingPathComponent(session.accountKey).appendingPathComponent("cloud.sqlite"))
        if let epoch = try database?.meta("epoch"), epoch != session.serverEpoch { try database?.preserveRecovery() }
        status = try database?.recoveryRequired() == true ? "需恢复数据" : "等待同步"
    }
    func login(base: String, email: String, password: String, deviceName: String) async throws {
        transition(); let operation = generation
        try credentials.clear(); session = nil; database = nil; CloudCache.clear(directory: directory)
        let client = try clientFactory(base)
        let value = try await client.login(email: email, password: password, deviceName: deviceName)
        guard generation == operation else { throw CancellationError() }
        try credentials.save(value)
        CloudCache.clear(directory: directory)
        session = value; try open(value); failures = 0; nextAttempt = .distantPast
    }
    func logout() async throws {
        let captured = session
        transition(); let operation = generation
        // Detach local state before waiting on the old device's revocation.
        try credentials.clear(); session = nil; database = nil; CloudCache.clear(directory: directory); status = "需登录"
        var revocationError: Error?
        if let captured = captured {
            do { let _: JSONValue = try await client(captured).call(JSONValue.self, path: "/v1/auth/logout", method: "POST", statuses: [200, 204]) }
            catch { revocationError = error }
        }
        guard generation == operation else { return }
        if let error = revocationError { throw GoutouError.invalid("本机已退出；设备撤销请求未确认，可重新登录后在设备列表撤销。\(error.localizedDescription)") }
    }
    func select(_ uid: String) throws { try database?.setMeta("selected_profile", uid); try publish() }
    func selectedProfile() throws -> String? { try database?.meta("selected_profile") }
    func publish() throws {
        guard let db = database, let session = session else { return }
        let profiles = try db.visibleRecords("profiles").map(\.relationship)
        var selected = try selectedProfile()
        if !profiles.contains(where: { $0.id == selected }) { selected = profiles.first?.id; try db.setMeta("selected_profile", selected ?? "") }
        try CloudCache.atomic(CloudSnapshot(accountKey: session.accountKey, profiles: profiles, contexts: try db.contexts(), selectedProfileUid: selected), to: directory.appendingPathComponent("snapshot.json"))
    }
    func sync(manual: Bool = false) async throws {
        guard !syncing else { return }
        guard let db = database, var session = session else { throw CloudError.signIn }
        guard !(try db.recoveryRequired()) else { throw CloudError.recovery }
        if !manual && Date() < nextAttempt { return }
        let capturedGeneration = generation, syncId = UUID()
        activeSync = syncId
        syncing = true; status = "同步中"
        defer { if activeSync == syncId { syncing = false; activeSync = nil } }
        do {
            let client = try client(session)
            let refreshed: JSONValue = try await client.call(JSONValue.self, path: "/v1/auth/refresh", method: "POST")
            try checkCurrent(session, generation: capturedGeneration)
            guard let token = refreshed.object?["token"]?.string else { throw CloudError.protocolError }
            session.token = token; client.session = session; self.session = session; try credentials.save(session)
            let config: JSONValue = try await client.call(JSONValue.self, path: "/v1/config")
            try checkCurrent(session, generation: capturedGeneration)
            guard config.object?["api_version"]?.int == 1, let epoch = config.object?["server_epoch"]?.string else { throw CloudError.protocolError }
            guard epoch == session.serverEpoch else { throw CloudError.recovery }
            guard config.object?["sync_ready"] == .bool(true) else { throw GoutouError.invalid("云端同步尚未就绪") }
            try await pull(client, db: db, epoch: epoch, generation: capturedGeneration)
            try checkCurrent(session, generation: capturedGeneration)
            while true {
                let operations = Array(try db.operations(includeHeld: false).prefix(50))
                if operations.isEmpty { break }
                let body: JSONValue = .object(["server_epoch": .string(epoch), "operations": .array(try operations.map { try CloudJSON.value($0) })])
                let response = try await client.call(CloudPushResponse.self, path: "/v1/sync/push", method: "POST", body: body)
                try checkCurrent(session, generation: capturedGeneration)
                guard response.serverEpoch == epoch, Set(response.results.map(\.opId)) == Set(operations.map(\.opId)) else { throw CloudError.protocolError }
                try db.applyPush(response)
                if response.results.contains(where: { $0.status != "applied" }) { break }
            }
            try await pull(client, db: db, epoch: epoch, generation: capturedGeneration)
            try checkCurrent(session, generation: capturedGeneration)
            try publish()
            failures = 0; nextAttempt = .distantPast
            let pending = try db.operations().count, conflicts = try db.conflicts().count
            status = conflicts > 0 ? "冲突 \(conflicts) 项 · 待上传 \(pending) 项" : pending > 0 ? "待上传 \(pending) 项" : "已同步 · \(CloudJSON.now())"
        } catch {
            // This task may have been a manual sync whose owner logged out while
            // URLSession was suspended. Its error/defer must not touch the new
            // account's vault, recovery flags, backoff, status or sync indicator.
            try checkCurrent(session, generation: capturedGeneration)
            if error is CancellationError { throw error }
            failures += 1; nextAttempt = Date().addingTimeInterval(min(300, pow(2, Double(min(failures, 8)))))
            if case CloudError.recovery = error { try db.preserveRecovery(); try credentials.clear(); self.session = nil; transition(); CloudCache.clear(directory: directory) }
            if case CloudError.http(401, _) = error { try credentials.clear(); self.session = nil; transition(); CloudCache.clear(directory: directory) }
            status = error.localizedDescription
            throw error
        }
    }
    private func pull(_ client: GatewayClient, db: CloudDatabase, epoch: String, generation: UUID) async throws {
        guard let captured = client.session else { throw CloudError.signIn }
        while true {
            let cursor = try db.cursor()
            var query = [URLQueryItem(name: "limit", value: "200")]
            if !cursor.isEmpty { query.append(.init(name: "cursor", value: cursor)) }
            let page = try await client.call(CloudPullResponse.self, path: "/v1/sync/pull", query: query)
            try checkCurrent(captured, generation: generation)
            guard page.serverEpoch == epoch, !page.hasMore || page.cursor != cursor else { throw CloudError.recovery }
            try db.applyPull(page)
            if !page.hasMore { return }
        }
    }
    func rebuild() throws {
        guard let db = database, let session = session else { throw CloudError.signIn }
        try db.rebuildAfterRecovery(epoch: session.serverEpoch)
        status = "恢复副本已保留，等待重新同步"
    }
    func submit(conversationUid: String, revision: Int, kind: String, parent: String?, config: AppConfig) async throws -> CloudPendingJob {
        guard let db = database, let session = session else { throw CloudError.signIn }
        guard !(try db.recoveryRequired()) else { throw CloudError.recovery }
        let value = CloudPendingJob(requestId: UUID().uuidString.lowercased(), conversationUid: conversationUid, contextRevision: revision, kind: kind, parentAnalysisId: parent, analysisId: nil, preferences: config.cloudPreferences)
        try db.savePending(value)
        return try await retryOriginal(value, config: config, session: session, db: db)
    }
    func retryOriginal(_ pending: CloudPendingJob, config: AppConfig) async throws -> CloudPendingJob {
        guard let db = database, let session = session else { throw CloudError.signIn }
        return try await retryOriginal(pending, config: config, session: session, db: db)
    }
    private func retryOriginal(_ pending: CloudPendingJob, config: AppConfig, session: CloudSession, db: CloudDatabase) async throws -> CloudPendingJob {
        let capturedGeneration = generation
        let job = try await client(session).create(pending, config: config)
        try checkCurrent(session, generation: capturedGeneration)
        guard job.conversationUid == pending.conversationUid, job.contextRevision == pending.contextRevision else { throw CloudError.protocolError }
        var saved = pending; saved.analysisId = job.analysisId; try db.savePending(saved); return saved
    }
    func query(_ pending: CloudPendingJob) async throws -> CloudJob {
        guard let session = session, let db = database, let id = pending.analysisId else { throw GoutouError.invalid("提交结果未确认；请手动用原请求重试，避免重复计费") }
        let capturedGeneration = generation
        let job = try await client(session).job(id)
        try checkCurrent(session, generation: capturedGeneration)
        guard job.conversationUid == pending.conversationUid, job.contextRevision == pending.contextRevision else { throw CloudError.protocolError }
        if ["succeeded", "failed", "cancelled", "indeterminate"].contains(job.status) { try db.removePending(pending.requestId) }
        return job
    }
    func evidence(analysisId: String, context: CloudContext) async throws -> CloudEvidenceDocument {
        guard let session = session, context.result?.analysisId == analysisId else { throw CloudError.signIn }
        let capturedGeneration = generation
        var value = try await client(session).evidence(analysisId)
        try checkCurrent(session, generation: capturedGeneration)
        guard value.analysisId == analysisId, value.profileUid == context.profileUid,
              value.conversationUid == context.conversationUid, value.contextRevision == context.contextRevision else { throw CloudError.protocolError }
        if value.evidence.isEmpty { value.evidence = context.result?.displayEvidence ?? [] }
        return value
    }
    func memoryHistory(uid: String, profileUid: String, beforeVersion: Int = 0) async throws -> CloudMemoryHistory {
        guard let session = session else { throw CloudError.signIn }
        let capturedGeneration = generation
        let value = try await client(session).memoryHistory(uid, beforeVersion: beforeVersion)
        try checkCurrent(session, generation: capturedGeneration)
        guard value.memoryUid == uid, value.profileUid == profileUid,
              !value.hasMore || (value.nextBeforeVersion > 0 && (beforeVersion == 0 || value.nextBeforeVersion < beforeVersion)),
              value.versions.allSatisfy({ $0.entityType == "memories" && $0.uid == uid && $0.deletedAt == nil && $0.payload["profile_uid"]?.string == profileUid }) else { throw CloudError.protocolError }
        return value
    }
}
