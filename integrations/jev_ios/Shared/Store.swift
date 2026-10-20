import Foundation
#if os(iOS)
import Security
#endif

struct KeyboardHandoff: Codable {
    var transcript: String
    var advice: Advice
    var relationship: Relationship
    var config: AppConfig
    var createdAt: Date

    func valid(at date: Date, relationship current: Relationship, config currentConfig: AppConfig) -> Bool {
        let age = date.timeIntervalSince(createdAt)
        return (0...300).contains(age) && current == relationship && currentConfig == config
    }
}

/// A single resumable round, never an archive or an authorization to analyze/insert.
/// Images, typing composition, credentials and confirmation state are excluded.
struct KeyboardSession: Codable {
    var transcript: String
    var advice: Advice?
    var relationship: Relationship
    var config: AppConfig
    var createdAt: Date

    func valid(at date: Date, relationship current: Relationship, config currentConfig: AppConfig) -> Bool {
        (0...300).contains(date.timeIntervalSince(createdAt)) &&
        current == relationship && currentConfig == config &&
        !transcript.trimmed.isEmpty && transcript.count <= 12_000
    }
}

final class GoutouStore {
    static var group: String { Bundle.main.object(forInfoDictionaryKey: "GoutouAppGroup") as? String ?? "group.ai.goutoujunshi.chatios" }
    private let defaults: UserDefaults
    private let container: URL

    init() throws {
        #if os(iOS)
        guard let container = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: Self.group),
              let defaults = UserDefaults(suiteName: Self.group) else {
            throw GoutouError.invalid("共享配置未连接：请在 Xcode 为 App 与键盘配置相同 App Group 和开发者 Team")
        }
        self.container = container
        self.defaults = defaults
        #else
        container = FileManager.default.temporaryDirectory.appendingPathComponent("goutou-ios-core-tests")
        defaults = UserDefaults(suiteName: "ai.goutoujunshi.core-tests")!
        #endif
        purgeExpiredHandoff()
        captureMailbox.purge(relationship: relationship(), config: config())
        _ = keyboardSession()
    }

    init(defaults: UserDefaults, container: URL) {
        self.defaults = defaults; self.container = container
        purgeExpiredHandoff()
        captureMailbox.purge(relationship: relationship(), config: config())
        _ = keyboardSession()
    }

    func config() -> AppConfig { decode("config", fallback: AppConfig()) }
    var cloudEnabled: Bool {
        get { defaults.bool(forKey: "cloudEnabled") }
        set { defaults.set(newValue, forKey: "cloudEnabled"); clearHandoff(); captureMailbox.clear(); clearKeyboardSession() }
    }
    var cloudServer: String {
        get { defaults.string(forKey: "cloudServer") ?? "https://api.zovoeh.cn" }
        set { defaults.set(newValue, forKey: "cloudServer") }
    }
    var cloudDirectory: URL { container.appendingPathComponent("cloud", isDirectory: true) }
    func saveConfig(_ value: AppConfig) throws {
        try value.validate()
        try save(value, key: "config")
        clearHandoff()
        captureMailbox.clear()
        clearKeyboardSession()
    }
    func relationship() -> Relationship { decode("relationship", fallback: Relationship(id: "current")) }
    func saveRelationship(_ value: Relationship, archive: Bool) throws {
        try value.validate()
        if archive {
            var profiles = archived()
            profiles.removeAll { $0.id == value.id }
            profiles.append(value)
            guard profiles.count <= 100 else { throw GoutouError.invalid("对象档案最多 100 个，请先清理") }
            try save(profiles, key: "profiles")
        }
        try save(value, key: "relationship")
        clearHandoff()
        captureMailbox.clear()
        clearKeyboardSession()
    }
    func archived() -> [Relationship] { decode("profiles", fallback: []) }
    func deleteProfile(_ id: String) throws {
        try save(archived().filter { $0.id != id }, key: "profiles")
        if relationship().id == id { try saveRelationship(Relationship(id: "current"), archive: false) }
    }
    func clearProfiles() {
        defaults.removeObject(forKey: "profiles")
        defaults.removeObject(forKey: "relationship")
        clearHandoff()
        captureMailbox.clear()
        clearKeyboardSession()
    }
    func setKeyboardStatus(fullAccess: Bool) {
        defaults.set(fullAccess, forKey: "keyboardFullAccess")
        defaults.set(Date().timeIntervalSince1970, forKey: "keyboardLastSeen")
    }
    var keyboardStatus: String {
        guard let seen = defaults.object(forKey: "keyboardLastSeen") as? Double, Date().timeIntervalSince1970 - seen < 86_400 else {
            return "尚未检测到键盘；请到聊天输入框切换一次"
        }
        return defaults.bool(forKey: "keyboardFullAccess") ? "最近已连接 · 完全访问已开启" : "最近已连接 · 需要开启完全访问"
    }
    private func save<T: Encodable>(_ value: T, key: String) throws { defaults.set(try JSONEncoder().encode(value), forKey: key) }
    private func decode<T: Decodable>(_ key: String, fallback: T) -> T {
        guard let data = defaults.data(forKey: key), let value = try? JSONDecoder().decode(T.self, from: data) else { return fallback }
        return value
    }

    /// This is an explicit one-time handoff, never an automatic chat archive.
    func handoff(_ value: KeyboardHandoff) throws {
        let data = try JSONEncoder().encode(value)
        guard data.count <= 512_000 else { throw GoutouError.invalid("本轮内容过大，无法交给键盘") }
        try FileManager.default.createDirectory(at: container, withIntermediateDirectories: true)
        let path = container.appendingPathComponent("handoff.json")
        #if os(iOS)
        try data.write(to: path, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        do {
            var excluded = path
            var resource = URLResourceValues()
            resource.isExcludedFromBackup = true
            try excluded.setResourceValues(resource)
        } catch { clearHandoff(); throw error }
        #else
        try data.write(to: path, options: .atomic)
        #endif
    }
    func takeHandoff() -> KeyboardHandoff? {
        let path = container.appendingPathComponent("handoff.json")
        defer { clearHandoff() }
        guard let size = try? path.resourceValues(forKeys: [.fileSizeKey]).fileSize, size <= 512_000,
              let data = try? Data(contentsOf: path), let result = try? JSONDecoder().decode(KeyboardHandoff.self, from: data),
              result.valid(at: Date(), relationship: relationship(), config: config()) else { return nil }
        return result
    }
    /// iOS cannot guarantee background deletion at an exact deadline. Expired data
    /// is unusable immediately and is removed when either process next opens it.
    func purgeExpiredHandoff() {
        let path = container.appendingPathComponent("handoff.json")
        guard FileManager.default.fileExists(atPath: path.path) else { return }
        guard let size = try? path.resourceValues(forKeys: [.fileSizeKey]).fileSize, size <= 512_000,
              let data = try? Data(contentsOf: path),
              let value = try? JSONDecoder().decode(KeyboardHandoff.self, from: data),
              value.valid(at: Date(), relationship: relationship(), config: config()) else {
            clearHandoff(); return
        }
    }
    func clearHandoff() { try? FileManager.default.removeItem(at: container.appendingPathComponent("handoff.json")) }
    func saveKeyboardSession(_ value: KeyboardSession) throws {
        guard value.valid(at: Date(), relationship: relationship(), config: config()) else {
            throw GoutouError.invalid("本轮暂存已过期或对象已变化，请重新读取")
        }
        let data = try JSONEncoder().encode(value)
        guard data.count <= 512_000 else { throw GoutouError.invalid("本轮内容过大，无法暂存") }
        try FileManager.default.createDirectory(at: container, withIntermediateDirectories: true)
        let path = container.appendingPathComponent("keyboard-session.json")
        #if os(iOS)
        try data.write(to: path, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        do {
            var file = path, values = URLResourceValues()
            values.isExcludedFromBackup = true
            try file.setResourceValues(values)
        } catch { clearKeyboardSession(); throw error }
        #else
        try data.write(to: path, options: .atomic)
        #endif
    }
    /// Read without consuming so dismissing/recreating the keyboard can resume.
    func keyboardSession() -> KeyboardSession? {
        let path = container.appendingPathComponent("keyboard-session.json")
        guard FileManager.default.fileExists(atPath: path.path) else { return nil }
        guard let size = try? path.resourceValues(forKeys: [.fileSizeKey]).fileSize, size <= 512_000,
              let data = try? Data(contentsOf: path), let value = try? JSONDecoder().decode(KeyboardSession.self, from: data),
              value.valid(at: Date(), relationship: relationship(), config: config()) else {
            clearKeyboardSession(); return nil
        }
        return value
    }
    func clearKeyboardSession() {
        try? FileManager.default.removeItem(at: container.appendingPathComponent("keyboard-session.json"))
    }
    var captureMailbox: CaptureMailbox { CaptureMailbox(container: container) }
}

#if !GOUTOU_CAPTURE
enum CredentialVault {
    static let services = ["deepseek", "openrouter", "jev"]
    #if os(iOS)
    private static func query(_ name: String) throws -> [String: Any] {
        guard services.contains(name), let group = Bundle.main.object(forInfoDictionaryKey: "GoutouKeychainGroup") as? String,
              !group.contains("$("), !group.isEmpty else { throw GoutouError.invalid("共享钥匙串配置未完成，请检查两个 target 的 Keychain Sharing") }
        return [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: "ai.goutoujunshi.ios." + name,
                kSecAttrAccount as String: "default", kSecAttrAccessGroup as String: group]
    }
    static func read(_ name: String) throws -> String {
        var query = try query(name)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecItemNotFound { return "" }
        guard status == errSecSuccess, let data = item as? Data, let value = String(data: data, encoding: .utf8) else {
            throw GoutouError.invalid("钥匙串读取失败（\(status)），请检查共享权限")
        }
        return value
    }
    static func write(_ value: String, name: String) throws {
        let value = value.trimmed
        guard !value.isEmpty, value.count <= 1024 else { throw GoutouError.invalid("请输入有效的 API Key") }
        let query = try query(name)
        let attributes: [String: Any] = [kSecValueData as String: Data(value.utf8),
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
        var status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            status = SecItemAdd(query.merging(attributes, uniquingKeysWith: { _, new in new }) as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw GoutouError.invalid("密钥保存失败（\(status)），请检查共享钥匙串签名") }
    }
    static func delete(_ name: String) throws {
        let status = SecItemDelete(try query(name) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { throw GoutouError.invalid("无法删除密钥（\(status)）") }
    }
    #else
    static func read(_ name: String) throws -> String { "" }
    static func write(_ value: String, name: String) throws { throw GoutouError.invalid("请在 iOS App 中配置密钥") }
    static func delete(_ name: String) throws {}
    #endif

    static func load() throws -> ProviderSecrets {
        try ProviderSecrets(deepseek: read("deepseek"), openrouter: read("openrouter"), jev: read("jev"))
    }
}
#endif
