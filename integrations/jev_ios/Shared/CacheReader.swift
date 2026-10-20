import Foundation

enum CloudCache {
    static func read(directory: URL, accountKey: String) throws -> CloudSnapshot? {
        let url = directory.appendingPathComponent("snapshot.json")
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        guard let size = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize, size <= 12_000_000 else { throw CloudError.protocolError }
        let snapshot = try CloudJSON.decoder.decode(CloudSnapshot.self, from: Data(contentsOf: url))
        return snapshot.belongs(to: accountKey) ? snapshot : nil
    }
    static func atomic<T: Encodable>(_ value: T, to url: URL) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let data = try CloudJSON.encoder.encode(value)
        #if os(iOS)
        try data.write(to: url, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        var file = url, values = URLResourceValues(); values.isExcludedFromBackup = true; try file.setResourceValues(values)
        #else
        try data.write(to: url, options: .atomic)
        #endif
    }
    static func clear(directory: URL) {
        // Visible account data is disposable. An ambiguous paid POST's request ID
        // is durable until its own account observes a terminal result.
        try? FileManager.default.removeItem(at: directory.appendingPathComponent("snapshot.json"))
    }
}
/// The extension never opens SQLite. This mailbox contains only the persisted
/// idempotency ID for an already synced context, so ambiguous POSTs are resumable.
struct CloudKeyboardJob: Codable {
    var accountKey: String; var pending: CloudPendingJob
    private static func url(directory: URL, accountKey: String) -> URL? {
        guard !accountKey.isEmpty, accountKey.count <= 128,
              accountKey.unicodeScalars.allSatisfy({ CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_" )).contains($0) }) else { return nil }
        return directory.appendingPathComponent("pending-jobs", isDirectory: true).appendingPathComponent(accountKey + ".json")
    }
    static func read(directory: URL, accountKey: String) -> CloudKeyboardJob? {
        guard let url = url(directory: directory, accountKey: accountKey) else { return nil }
        if let data = try? Data(contentsOf: url), let value = try? CloudJSON.decoder.decode(Self.self, from: data), value.accountKey == accountKey { return value }
        // Preserve pre-fix mailboxes without allowing another account to move or
        // execute them. The migration itself is atomic and only for this owner.
        let legacy = directory.appendingPathComponent("keyboard-job.json")
        guard let data = try? Data(contentsOf: legacy), let value = try? CloudJSON.decoder.decode(Self.self, from: data), value.accountKey == accountKey,
              (try? value.save(directory: directory)) != nil else { return nil }
        try? FileManager.default.removeItem(at: legacy)
        return value
    }
    func save(directory: URL) throws {
        guard let url = Self.url(directory: directory, accountKey: accountKey) else { throw CloudError.protocolError }
        try CloudCache.atomic(self, to: url)
    }
    static func remove(directory: URL, accountKey: String, requestId: String? = nil) {
        guard let url = url(directory: directory, accountKey: accountKey) else { return }
        if let requestId = requestId, read(directory: directory, accountKey: accountKey)?.pending.requestId != requestId { return }
        try? FileManager.default.removeItem(at: url)
    }
}
