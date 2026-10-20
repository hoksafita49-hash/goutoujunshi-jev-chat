import Foundation

enum CloudError: LocalizedError {
    case http(Int, String), recovery, protocolError, signIn
    var errorDescription: String? {
        switch self {
        case .http(let status, let code): return "云服务 HTTP \(status)：\(code)"
        case .recovery: return "服务器数据世代已变化。本机数据与待上传操作已保留，请重新登录并导出恢复副本后重建。"
        case .protocolError: return "云服务响应不符合版本 1 契约"
        case .signIn: return "请先在主 App 登录云账号"
        }
    }
}
private final class CloudRedirectGuard: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}
final class GatewayClient {
    var session: CloudSession?
    let base: URL
    private let network: URLSession
    init(base: String, configuration: URLSessionConfiguration = .ephemeral) throws {
        self.base = try Self.baseURL(base)
        let config = configuration
        config.timeoutIntervalForRequest = 30; config.timeoutIntervalForResource = 40
        config.urlCache = nil; config.httpCookieStorage = nil
        network = URLSession(configuration: config, delegate: CloudRedirectGuard(), delegateQueue: nil)
    }
    convenience init(session: CloudSession) throws { try self.init(base: session.baseUrl); self.session = session }
    deinit { network.invalidateAndCancel() }
    static func baseURL(_ input: String) throws -> URL {
        var text = input.trimmed
        while text.hasSuffix("/") { text.removeLast() }
        guard let url = URL(string: text), let host = url.host, url.user == nil, url.password == nil,
              url.query == nil, url.fragment == nil, url.path.isEmpty,
              url.scheme == "https" || (url.scheme == "http" && ["127.0.0.1", "localhost"].contains(host) && url.port == 18000) else {
            throw GoutouError.invalid("服务器须为 HTTPS 根地址，开发仅允许 http://127.0.0.1:18000")
        }
        return url
    }
    func request(path: String, method: String, query: [URLQueryItem] = [], body: JSONValue? = nil, authenticated: Bool = true) throws -> URLRequest {
        guard path.hasPrefix("/v1/"), !path.contains("?"), !path.contains("#"), !path.contains("..") else { throw CloudError.protocolError }
        var components = URLComponents(url: base, resolvingAgainstBaseURL: false)!
        components.path = path
        if !query.isEmpty { components.queryItems = query }
        guard let url = components.url else { throw CloudError.protocolError }
        var request = URLRequest(url: url); request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if authenticated {
            guard let session = session else { throw CloudError.signIn }
            request.setValue("Bearer " + session.token, forHTTPHeaderField: "Authorization")
            request.setValue(session.deviceId, forHTTPHeaderField: "X-Device-Session")
            request.setValue(session.deviceSecret, forHTTPHeaderField: "X-Device-Secret")
        }
        if let body = body { request.httpBody = try JSONEncoder().encode(body) }
        return request
    }
    func call<T: Decodable>(_ type: T.Type, path: String, method: String = "GET", body: JSONValue? = nil,
                            query: [URLQueryItem] = [], authenticated: Bool = true, statuses: Set<Int> = [200]) async throws -> T {
        let request = try request(path: path, method: method, query: query, body: body, authenticated: authenticated)
        let (data, response) = try await network.data(for: request)
        try Task.checkCancellation()
        guard let response = response as? HTTPURLResponse, data.count <= 12_000_000 else { throw CloudError.protocolError }
        guard statuses.contains(response.statusCode) else {
            if response.statusCode == 401, let saved = try? SessionVault.read(), saved.accountKey == session?.accountKey, saved.deviceId == session?.deviceId {
                try? SessionVault.clear()
            }
            let value = try? JSONDecoder().decode(JSONValue.self, from: data)
            let code = value?.object?["error"]?.object?["code"]?.string ?? "invalid_response"
            if response.statusCode == 409 && code == "server_epoch_changed" { throw CloudError.recovery }
            throw CloudError.http(response.statusCode, code)
        }
        return try CloudJSON.decoder.decode(type, from: data.isEmpty ? Data("{}".utf8) : data)
    }
    func login(email: String, password: String, deviceName: String) async throws -> CloudSession {
        let value = try await call(CloudLogin.self, path: "/v1/auth/login", method: "POST", body: .object([
            "email": .string(email.trimmed), "password": .string(password), "device_name": .string(deviceName), "platform": .string("ios")]), authenticated: false)
        guard value.apiVersion == 1 else { throw CloudError.protocolError }
        return value.session(base: base.absoluteString)
    }
    func create(_ pending: CloudPendingJob, config: AppConfig) async throws -> CloudJob {
        var body: [String: JSONValue] = ["request_id": .string(pending.requestId), "conversation_uid": .string(pending.conversationUid),
            "context_revision": .number(Double(pending.contextRevision)), "kind": .string(pending.kind),
            "preferences": .object(pending.preferences ?? config.cloudPreferences)]
        if let parent = pending.parentAnalysisId { body["parent_analysis_id"] = .string(parent) }
        return try await call(CloudJob.self, path: "/v1/analyses", method: "POST", body: .object(body), statuses: [202])
    }
    func job(_ id: String) async throws -> CloudJob {
        guard UUID(uuidString: id) != nil else { throw CloudError.protocolError }
        return try await call(CloudJob.self, path: "/v1/analyses/" + id)
    }
    func evidence(_ id: String) async throws -> CloudEvidenceDocument {
        guard UUID(uuidString: id) != nil else { throw CloudError.protocolError }
        return try await call(CloudEvidenceDocument.self, path: "/v1/analyses/" + id + "/evidence")
    }
    func memoryHistory(_ uid: String, beforeVersion: Int = 0) async throws -> CloudMemoryHistory {
        guard UUID(uuidString: uid) != nil, beforeVersion >= 0 else { throw CloudError.protocolError }
        return try await call(CloudMemoryHistory.self, path: "/v1/memories/" + uid + "/history", query: [.init(name: "before_version", value: String(beforeVersion)), .init(name: "limit", value: "50")])
    }
}
