import Foundation
import CryptoKit

enum CloudJSON {
    static var encoder: JSONEncoder { let value = JSONEncoder(); value.keyEncodingStrategy = .convertToSnakeCase; value.outputFormatting = [.sortedKeys]; return value }
    static var decoder: JSONDecoder { let value = JSONDecoder(); value.keyDecodingStrategy = .convertFromSnakeCase; return value }
    static func value<T: Encodable>(_ value: T) throws -> JSONValue { try JSONDecoder().decode(JSONValue.self, from: encoder.encode(value)) }
    static func decode<T: Decodable>(_ type: T.Type, value: JSONValue) throws -> T { try decoder.decode(type, from: JSONEncoder().encode(value)) }
    static func now() -> String { ISO8601DateFormatter().string(from: Date()) }
}

enum JSONValue: Codable, Equatable {
    case string(String), number(Double), bool(Bool), object([String: JSONValue]), array([JSONValue]), null
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let v = try? c.decode(Bool.self) { self = .bool(v) }
        else if let v = try? c.decode(Double.self) { self = .number(v) }
        else if let v = try? c.decode(String.self) { self = .string(v) }
        else if let v = try? c.decode([String: JSONValue].self) { self = .object(v) }
        else { self = .array(try c.decode([JSONValue].self)) }
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let v): try c.encode(v)
        case .number(let v): try c.encode(v)
        case .bool(let v): try c.encode(v)
        case .object(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        case .null: try c.encodeNil()
        }
    }
    var string: String? { if case .string(let v) = self { return v }; return nil }
    var int: Int? { if case .number(let v) = self, v.isFinite, abs(v) < Double(Int.max) { return Int(v) }; return nil }
    var object: [String: JSONValue]? { if case .object(let v) = self { return v }; return nil }
}

struct CloudSession: Codable, Equatable {
    var baseUrl: String
    var token: String
    var userId: String
    var email: String
    var deviceId: String
    var deviceSecret: String
    var serverEpoch: String
    var accountKey: String { SHA256.hash(data: Data((baseUrl + "|" + userId).utf8)).map { String(format: "%02x", $0) }.joined() }
}
struct CloudLogin: Codable {
    struct User: Codable { var id: String; var email: String }
    struct Device: Codable { var id: String; var name: String; var expiresAt: String }
    var apiVersion: Int; var token: String; var user: User; var device: Device; var deviceSecret: String; var serverEpoch: String
    func session(base: String) -> CloudSession { .init(baseUrl: base, token: token, userId: user.id, email: user.email, deviceId: device.id, deviceSecret: deviceSecret, serverEpoch: serverEpoch) }
}
struct CloudRecord: Codable, Equatable, Identifiable {
    var entityType: String; var uid: String; var version: Int; var createdAt: String; var updatedAt: String; var deletedAt: String?; var payload: [String: JSONValue]
    var id: String { entityType + ":" + uid }
    var relationship: Relationship {
        Relationship(id: uid, label: payload["name"]?.string ?? "未命名", stage: payload["stage"]?.string ?? "未填写", goal: payload["goal"]?.string ?? "自然接话", background: payload["background"]?.string ?? "", notes: payload["notes"]?.string ?? "")
    }
}
extension Relationship {
    var cloudPayload: [String: JSONValue] { ["name": .string(label), "stage": .string(stage), "goal": .string(goal), "background": .string(background), "notes": .string(notes)] }
}
struct CloudOperation: Codable, Equatable, Identifiable {
    var opId: String = UUID().uuidString.lowercased(); var entityType: String; var entityUid: String; var baseVersion: Int; var action: String; var payload: [String: JSONValue]
    var id: String { opId }
}
struct CloudPushResponse: Codable {
    struct Receipt: Codable { var opId: String; var status: String; var record: CloudRecord?; var code: String?; var seq: Int? }
    var serverEpoch: String; var results: [Receipt]
}
struct CloudPullResponse: Codable {
    struct Change: Codable { var seq: Int; var record: CloudRecord }
    var serverEpoch: String; var changes: [Change]; var cursor: String; var hasMore: Bool
}
struct CloudConflict: Identifiable {
    var operation: CloudOperation; var remote: CloudRecord?; var code: String
    var id: String { operation.opId }
}
struct CloudPendingJob: Codable, Identifiable {
    var requestId: String; var conversationUid: String; var contextRevision: Int; var kind: String; var parentAnalysisId: String?; var analysisId: String?
    var preferences: [String: JSONValue]? = nil
    var id: String { requestId }
}
extension AppConfig {
    var cloudPreferences: [String: JSONValue] { ["tone": .string(tone), "length": .string(length), "count": .number(Double(count)), "style": .string("")] }
}
struct CloudJob: Codable {
    struct JobError: Codable { var code: String }
    var analysisId: String; var status: String; var conversationUid: String; var contextRevision: Int
    var result: CloudAnalysisResult?; var error: JobError?
}
struct CloudAnalysisResult: Codable, Equatable {
    struct CandidateRow: Codable, Equatable { var candidateId: String; var text: String; var reason: String?; var weight: Double? }
    var schemaVersion: Int; var analysisId: String; var conversationUid: String; var contextRevision: Int
    var facts: [String]; var hypotheses: [String]; var unknowns: [String]; var strategy: String; var stopCondition: String
    var candidates: [CandidateRow]; var rankingStatus: String; var usage: [String: JSONValue]; var pipelineVersion: String; var text: String?
    func matches(conversationUid: String, revision: Int) -> Bool { self.conversationUid == conversationUid && contextRevision == revision }
    func advice() -> Advice {
        Advice(support: text ?? "服务端统一分析", intent: "请结合可见事实判断，未知保持未知。", intentConfidence: nil,
               facts: facts, hypotheses: hypotheses, unknowns: unknowns, strategy: Strategy(rawValue: strategy) ?? .clarify,
               recommendation: text ?? (facts.first ?? "查看候选及依据"), nextStep: "核对当前对象后自行选择。", stopCondition: stopCondition, question: "",
               candidates: candidates.map { row in
                   let value = rankingStatus == "available" ? row.weight : nil
                   return Candidate(text: row.text, reason: row.reason ?? "未提供", tradeoff: "未提供", weight: value.flatMap { $0.isFinite && (0...1).contains($0) ? Int(($0 * 100).rounded()) : nil }, stableId: row.candidateId)
               }, decision: nil, rankingStatus: rankingStatus)
    }
}
struct CloudContext: Codable, Equatable, Identifiable {
    var profileUid: String; var conversationUid: String; var contextRevision: Int; var transcript: String; var result: CloudAnalysisResult?
    var id: String { conversationUid }
}
struct CloudSnapshot: Codable {
    var accountKey: String; var profiles: [Relationship]; var contexts: [CloudContext]; var selectedProfileUid: String?
    var createdAt = Date()
    func belongs(to key: String) -> Bool { accountKey == key }
}
