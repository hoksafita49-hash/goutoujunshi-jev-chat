import Foundation
#if os(iOS)
import Security
#endif

struct CloudSessionStorage {
    var read: () throws -> CloudSession?
    var save: (CloudSession) throws -> Void
    var clear: () throws -> Void
    static var keychain: Self { .init(read: { try SessionVault.read() }, save: { try SessionVault.save($0) }, clear: { try SessionVault.clear() }) }
}

enum SessionVault {
    #if os(iOS)
    private static func query() throws -> [String: Any] {
        guard let group = Bundle.main.object(forInfoDictionaryKey: "GoutouKeychainGroup") as? String,
              !group.isEmpty, !group.contains("$(") else { throw GoutouError.invalid("共享账号钥匙串未配置") }
        return [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: "ai.goutoujunshi.ios.cloud-session",
                kSecAttrAccount as String: "current", kSecAttrAccessGroup as String: group]
    }
    static func read() throws -> CloudSession? {
        var query = try query(); query[kSecReturnData as String] = true; query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data else { throw GoutouError.invalid("账号钥匙串读取失败（\(status)）") }
        return try CloudJSON.decoder.decode(CloudSession.self, from: data)
    }
    static func save(_ session: CloudSession) throws {
        let query = try query(), data = try CloudJSON.encoder.encode(session)
        let attributes: [String: Any] = [kSecValueData as String: data, kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
        var status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound { status = SecItemAdd(query.merging(attributes, uniquingKeysWith: { _, new in new }) as CFDictionary, nil) }
        guard status == errSecSuccess else { throw GoutouError.invalid("账号保存失败（\(status)）") }
    }
    static func clear() throws {
        let status = SecItemDelete(try query() as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { throw GoutouError.invalid("账号清除失败（\(status)）") }
    }
    #else
    static func read() throws -> CloudSession? { nil }
    static func save(_ session: CloudSession) throws { throw CloudError.signIn }
    static func clear() throws {}
    #endif
}
