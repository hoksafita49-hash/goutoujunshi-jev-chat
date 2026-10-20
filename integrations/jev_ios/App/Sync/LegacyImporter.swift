import Foundation

struct LegacyImporter {
    let database: CloudDatabase
    func preview(_ profiles: [Relationship]) throws -> [Relationship] {
        try profiles.filter { try database.legacyUid($0.id) == nil }
    }
    func confirm(_ profiles: [Relationship]) throws { try database.importProfiles(profiles) }
}
