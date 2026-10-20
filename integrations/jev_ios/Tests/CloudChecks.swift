import Foundation

enum CloudCheckError: Error { case failed(String) }
final class CloudURLProtocol: URLProtocol {
    static var reply: (Int, Data) = (200, Data("{}".utf8))
    static var calls = 0
    static var handle: ((CloudURLProtocol) -> Void)?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.calls += 1
        if let handle = Self.handle { handle(self); return }
        complete(status: Self.reply.0, data: Self.reply.1)
    }
    func complete(status: Int, data: Data) {
        let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1", headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data); client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@main
struct CloudChecks {
    static var count = 0
    static func check(_ name: String, _ value: @autoclosure () throws -> Bool) throws {
        count += 1
        guard try value() else { throw CloudCheckError.failed(name) }
    }
    static func rejects(_ name: String, _ block: () throws -> Void) throws {
        count += 1
        do { try block() } catch { return }
        throw CloudCheckError.failed(name)
    }
    static func main() async {
        do {
            try endpoints()
            try recordsAndMigration()
            try historicalPull()
            try keyboardMailboxes()
            try snapshotsAndResults()
            try fixtures()
            try await requests()
            try await staleSessionFailure()
            print("PASS: \(count) cloud contract, SQLite, migration and account isolation checks")
        } catch { fputs("FAIL: \(error)\n", stderr); exit(1) }
    }
    static func historicalPull() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let db = try CloudDatabase(url: directory.appendingPathComponent("history.sqlite"))
        let uid = UUID().uuidString.lowercased()
        try db.applyPull(.init(serverEpoch: "epoch", changes: [.init(seq: 1, record: record(uid, version: 1, name: "v1"))], cursor: "old-cursor", hasMore: false))
        for version in [2, 3] {
            try db.stage(entityType: "profiles", uid: uid, payload: record(uid, version: version, name: "v\(version)").payload)
            let accepted = try db.operations().first!
            try db.applyPush(.init(serverEpoch: "epoch", results: [.init(opId: accepted.opId, status: "applied", record: record(uid, version: version, name: "v\(version)"), code: nil, seq: version)]))
        }
        try db.stage(entityType: "profiles", uid: uid, payload: record(uid, version: 3, name: "offline-v4").payload)
        let original = try db.operations().first!
        try db.applyPull(.init(serverEpoch: "epoch", changes: [.init(seq: 1, record: record(uid, version: 1, name: "v1")), .init(seq: 2, record: record(uid, version: 2, name: "v2"))], cursor: "historical-page", hasMore: true))
        try check("Historical replay cannot downgrade push mirror", try db.remoteRecords("profiles").first?.version == 3)
        try check("Historical replay keeps dirty operation pending at base 3", try db.operations(includeHeld: false).first == original && db.conflicts().isEmpty)
        try check("Historical replay keeps local visible edit", try db.visibleRecords("profiles").first?.payload["name"]?.string == "offline-v4")
        try check("Historical page still advances its own cursor", try db.cursor() == "historical-page")
        var deleted = record(uid, version: 4, name: ""); deleted.deletedAt = "2026-10-21T02:00:00Z"; deleted.payload = [:]
        try db.applyPull(.init(serverEpoch: "epoch", changes: [.init(seq: 4, record: deleted)], cursor: "deleted", hasMore: false))
        try db.applyPull(.init(serverEpoch: "epoch", changes: [.init(seq: 2, record: record(uid, version: 2, name: "old"))], cursor: "old-replay", hasMore: false))
        try check("Historical replay cannot resurrect tombstone", try db.visibleRecords("profiles").isEmpty && db.remoteRecords("profiles").first?.version == 4)
    }
    static func keyboardMailboxes() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let a = CloudKeyboardJob(accountKey: "account-a", pending: .init(requestId: UUID().uuidString.lowercased(), conversationUid: UUID().uuidString.lowercased(), contextRevision: 3, kind: "analysis", parentAnalysisId: nil, analysisId: nil))
        let b = CloudKeyboardJob(accountKey: "account-b", pending: .init(requestId: UUID().uuidString.lowercased(), conversationUid: UUID().uuidString.lowercased(), contextRevision: 1, kind: "analysis", parentAnalysisId: nil, analysisId: nil))
        try a.save(directory: directory)
        try check("Other account cannot read ambiguous paid request", CloudKeyboardJob.read(directory: directory, accountKey: "account-b") == nil)
        try CloudCache.atomic(CloudSnapshot(accountKey: "account-a", profiles: [], contexts: [], selectedProfileUid: nil), to: directory.appendingPathComponent("snapshot.json"))
        CloudCache.clear(directory: directory)
        try check("Logout clears visible snapshot", try CloudCache.read(directory: directory, accountKey: "account-a") == nil)
        try check("Same-account relogin retains original paid request ID", CloudKeyboardJob.read(directory: directory, accountKey: "account-a")?.pending.requestId == a.pending.requestId)
        CloudKeyboardJob.remove(directory: directory, accountKey: "account-a", requestId: "late-completion-of-another-request")
        try check("Late completion cannot delete a newer same-account request", CloudKeyboardJob.read(directory: directory, accountKey: "account-a")?.pending.requestId == a.pending.requestId)
        try b.save(directory: directory); CloudCache.clear(directory: directory)
        try check("Different accounts retain separate pending mailboxes", CloudKeyboardJob.read(directory: directory, accountKey: "account-a")?.pending.requestId == a.pending.requestId && CloudKeyboardJob.read(directory: directory, accountKey: "account-b")?.pending.requestId == b.pending.requestId)
        CloudKeyboardJob.remove(directory: directory, accountKey: "account-b")
        try check("Completing B never removes A pending request", CloudKeyboardJob.read(directory: directory, accountKey: "account-a")?.pending.requestId == a.pending.requestId)
    }
    @MainActor
    static func staleSessionFailure() async throws {
        final class MemoryCredentials { var value: CloudSession? }
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory); CloudURLProtocol.handle = nil }
        let memory = MemoryCredentials()
        let a = CloudSession(baseUrl: "https://api.zovoeh.cn", token: "a", userId: "owner-a", email: "a@example.com", deviceId: "device-a", deviceSecret: "a-secret", serverEpoch: "epoch")
        memory.value = a
        let credentials = CloudSessionStorage(read: { memory.value }, save: { memory.value = $0 }, clear: { memory.value = nil })
        let coordinator = SyncCoordinator(directory: directory, credentials: credentials, clientFactory: { base in
            let configuration = URLSessionConfiguration.ephemeral; configuration.protocolClasses = [CloudURLProtocol.self]
            return try GatewayClient(base: base, configuration: configuration)
        })
        try coordinator.restore()
        try coordinator.database!.stage(entityType: "profiles", uid: UUID().uuidString.lowercased(), payload: record(UUID().uuidString.lowercased(), version: 1, name: "A private edit").payload)
        let aDatabase = coordinator.database!
        var heldA: CloudURLProtocol?, heldB: CloudURLProtocol?
        var startedA: CheckedContinuation<Void, Never>?, startedB: CheckedContinuation<Void, Never>?
        CloudURLProtocol.handle = { connection in
            Task { @MainActor in
                let path = connection.request.url!.path
                if path == "/v1/auth/refresh" {
                    if connection.request.value(forHTTPHeaderField: "X-Device-Session") == "device-a" { heldA = connection; startedA?.resume(); startedA = nil }
                    else { heldB = connection; startedB?.resume(); startedB = nil }
                    return
                }
                let data: Data
                if path == "/v1/auth/login" { data = Data(#"{"api_version":1,"token":"b","user":{"id":"owner-b","email":"b@example.com"},"device":{"id":"device-b","name":"iPhone","expires_at":"2026-11-21T00:00:00Z"},"device_secret":"b-secret","server_epoch":"epoch"}"#.utf8) }
                else if path == "/v1/config" { data = Data(#"{"api_version":1,"sync_ready":true,"server_epoch":"epoch"}"#.utf8) }
                else if path == "/v1/sync/pull" { data = Data(#"{"server_epoch":"epoch","changes":[],"cursor":"b-cursor","has_more":false}"#.utf8) }
                else { data = Data("{}".utf8) }
                connection.complete(status: 200, data: data)
            }
        }
        let oldSync = Task { do { try await coordinator.sync(manual: true) } catch {} }
        await withCheckedContinuation { startedA = $0 }
        try await coordinator.logout()
        try await coordinator.login(base: a.baseUrl, email: "b@example.com", password: "test", deviceName: "iPhone")
        let bSync = Task { do { try await coordinator.sync(manual: true) } catch {} }
        await withCheckedContinuation { startedB = $0 }
        heldA!.complete(status: 401, data: Data(#"{"error":{"code":"device_revoked"}}"#.utf8))
        await oldSync.value
        try check("Late A 401 cannot clear B session or credentials", coordinator.session?.userId == "owner-b" && memory.value?.userId == "owner-b")
        try check("Late A defer cannot clear B in-flight sync state", coordinator.syncing && coordinator.status == "同步中")
        try check("Late A failure preserves A outbox and isolates B database", try aDatabase.operations().count == 1 && coordinator.database!.operations().isEmpty)
        heldB!.complete(status: 200, data: Data(#"{"token":"b-refreshed"}"#.utf8))
        await bSync.value
        try check("Current B sync completes after stale failure", coordinator.session?.token == "b-refreshed" && !coordinator.syncing)
    }
    static func endpoints() throws {
        try check("HTTPS gateway", try GatewayClient.baseURL("https://api.zovoeh.cn/").absoluteString == "https://api.zovoeh.cn")
        try check("Local SSH tunnel", try GatewayClient.baseURL("http://127.0.0.1:18000").host == "127.0.0.1")
        for value in ["http://example.com", "https://u:p@example.com", "https://example.com?q=x", "https://example.com/#x", "https://example.com/v1"] {
            try rejects("Reject unsafe origin \(value)") { _ = try GatewayClient.baseURL(value) }
        }
    }
    static func record(_ uid: String, version: Int, name: String) -> CloudRecord {
        CloudRecord(entityType: "profiles", uid: uid, version: version, createdAt: "2026-10-21T00:00:00Z", updatedAt: "2026-10-21T00:00:00Z", deletedAt: nil,
                    payload: ["name": .string(name), "stage": .string("初识"), "goal": .string("自然接话"), "background": .string(""), "notes": .string("")])
    }
    static func recordsAndMigration() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let db = try CloudDatabase(url: folder.appendingPathComponent("a.sqlite"))
        let uid = UUID().uuidString.lowercased()
        try db.applyPull(CloudPullResponse(serverEpoch: "epoch", changes: [.init(seq: 1, record: record(uid, version: 1, name: "remote"))], cursor: "page1", hasMore: false))
        var edited = record(uid, version: 1, name: "local").payload
        edited["notes"] = .string("offline")
        try db.stage(entityType: "profiles", uid: uid, payload: edited)
        let op = try db.operations().first!
        let visible = try db.visibleRecords("profiles")
        try check("Atomic edit queues base version", op.baseVersion == 1 && visible.first?.payload["name"]?.string == "local")
        try db.applyPull(CloudPullResponse(serverEpoch: "epoch", changes: [.init(seq: 2, record: record(uid, version: 2, name: "new remote"))], cursor: "page2", hasMore: false))
        try check("Pull preserves dirty overlay", try db.visibleRecords("profiles").first?.payload["name"]?.string == "local")
        try db.applyPush(CloudPushResponse(serverEpoch: "epoch", results: [.init(opId: op.opId, status: "conflict", record: record(uid, version: 2, name: "new remote"), code: nil, seq: nil)]))
        try check("Conflict preserves outbox and remote", try db.conflicts().count == 1 && db.operations().count == 1)
        try check("Push never advances pull cursor", try db.cursor() == "page2")
        try db.resolve(opId: op.opId, useLocal: true)
        try check("Explicit conflict choice rebases new op", try db.operations().first?.baseVersion == 2 && db.operations().first?.opId != op.opId)
        let tombstone = CloudRecord(entityType: "profiles", uid: uid, version: 3, createdAt: "", updatedAt: "", deletedAt: "2026-10-21T01:00:00Z", payload: [:])
        try db.applyPull(.init(serverEpoch: "epoch", changes: [.init(seq: 3, record: tombstone)], cursor: "page3", hasMore: false))
        try check("Tombstone hides dirty entity", try db.visibleRecords("profiles").isEmpty)
        let old = Relationship(id: "old-local-id", label: "同名")
        let importer = LegacyImporter(database: db)
        try check("Migration preview writes nothing", try importer.preview([old]).count == 1 && db.legacyUid("old-local-id") == nil)
        try importer.confirm([old]); let migrated = try db.legacyUid("old-local-id")
        try importer.confirm([old])
        let imported = try db.visibleRecords("profiles")
        try check("Confirmed migration has stable UUID and no duplicate", migrated != nil && UUID(uuidString: migrated!) != nil && imported.count == 1)
        let context = try db.confirmConversation(profileUid: migrated!, transcript: "我：好\n对方：好\n对方：好", source: "手动输入")
        try check("Identical short messages preserved", try db.visibleRecords("messages").filter { $0.payload["conversation_uid"]?.string == context }.count == 3)
        try check("Confirmed messages only", try db.visibleRecords("messages").allSatisfy { $0.payload["confirmed"] == .bool(true) })
        let other = try CloudDatabase(url: folder.appendingPathComponent("b.sqlite"))
        try check("Separate account database", try other.operations().isEmpty && other.visibleRecords("profiles").isEmpty)
        try db.savePending(CloudPendingJob(requestId: UUID().uuidString.lowercased(), conversationUid: context, contextRevision: 1, kind: "analysis", parentAnalysisId: nil, analysisId: nil))
        try check("Pending request survives reopen", try CloudDatabase(url: folder.appendingPathComponent("a.sqlite")).pendingJobs().count == 1)
        try db.preserveRecovery()
        try check("Epoch recovery holds upload", try db.recoveryRequired())
    }
    static func snapshotsAndResults() throws {
        let raw = #"{"schema_version":1,"analysis_id":"a","conversation_uid":"c","context_revision":2,"facts":[],"hypotheses":[],"unknowns":[],"strategy":"降压","stop_condition":"停止","candidates":[{"candidate_id":"first","text":"好","reason":null,"weight":null},{"candidate_id":"second","text":"嗯","reason":"接住","weight":null}],"ranking_status":"unavailable","usage":{},"pipeline_version":"cloud-v1"}"#
        let result = try CloudJSON.decoder.decode(CloudAnalysisResult.self, from: Data(raw.utf8))
        try check("Server candidate IDs and order preserved", result.candidates.map(\.candidateId) == ["first", "second"])
        try check("Null weights stay nil", result.advice().candidates.allSatisfy { $0.weight == nil })
        var available = result; available.rankingStatus = "available"; available.candidates[0].weight = 0.6
        try check("Relative weights display as percentages", available.advice().candidates[0].weight == 60)
        let snapshot = CloudSnapshot(accountKey: "one", profiles: [], contexts: [], selectedProfileUid: nil)
        try check("Snapshot account validation", snapshot.belongs(to: "one") && !snapshot.belongs(to: "two"))
        try check("Delayed context result rejected", !result.matches(conversationUid: "other", revision: 2))
    }
    static func fixtures() throws {
        let root = URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent("../../contracts/fixtures")
        let result = try CloudJSON.decoder.decode(CloudAnalysisResult.self, from: Data(contentsOf: root.appendingPathComponent("cloud-v1-result.json")))
        try check("Shared fixture keeps unknown fields and candidate order", result.candidates.first?.candidateId == "30000000-0000-4000-8000-000000000002" && result.candidates.allSatisfy { $0.weight == nil })
        let page = try CloudJSON.decoder.decode(CloudPullResponse.self, from: Data(contentsOf: root.appendingPathComponent("cloud-v1-sync-page.json")))
        try check("Shared sync page decodes opaque cursor", !page.cursor.isEmpty && !page.changes.isEmpty)
    }
    static func requests() async throws {
        let session = CloudSession(baseUrl: "https://api.zovoeh.cn", token: "user-jwt", userId: "owner", email: "a@example.com", deviceId: "device", deviceSecret: "secret", serverEpoch: "epoch")
        let request = try GatewayClient(session: session).request(path: "/v1/sync/pull", method: "GET", query: [.init(name: "cursor", value: "opaque+/=")])
        try check("All device auth headers", request.value(forHTTPHeaderField: "Authorization") == "Bearer user-jwt" && request.value(forHTTPHeaderField: "X-Device-Session") == "device" && request.value(forHTTPHeaderField: "X-Device-Secret") == "secret")
        try check("Opaque cursor encoded as query", URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems?.first?.value == "opaque+/=")
        try rejects("Cannot redirect credentials to another origin") { _ = try GatewayClient(session: session).request(path: "https://other.example", method: "GET") }
        let job = try CloudJSON.decoder.decode(CloudJob.self, from: Data(#"{"analysis_id":"a","status":"queued","conversation_uid":"c","context_revision":1}"#.utf8))
        try check("202 job response permits absent result", job.result == nil && job.analysisId == "a")
        let configuration = URLSessionConfiguration.ephemeral; configuration.protocolClasses = [CloudURLProtocol.self]
        let client = try GatewayClient(base: session.baseUrl, configuration: configuration); client.session = session
        CloudURLProtocol.reply = (202, Data(#"{"analysis_id":"a","status":"queued","conversation_uid":"c","context_revision":1}"#.utf8))
        let accepted = try await client.call(CloudJob.self, path: "/v1/analyses", method: "POST", statuses: [202])
        try check("Transport accepts 202", accepted.status == "queued")
        CloudURLProtocol.reply = (401, Data(#"{"error":{"code":"device_revoked"}}"#.utf8))
        do {
            let _: JSONValue = try await client.call(JSONValue.self, path: "/v1/config")
            throw CloudCheckError.failed("401 must fail")
        } catch CloudError.http(let status, let code) { try check("401 preserves error code", status == 401 && code == "device_revoked") }
        try check("No implicit paid request retry", CloudURLProtocol.calls == 2)
    }
}
