import Foundation

public struct ProtectionAuditSnapshot: Equatable, Sendable {
    public let blockedCount: Int
    public let lastCategory: String?
    public let lastAt: String?

    public init(blockedCount: Int = 0, lastCategory: String? = nil, lastAt: String? = nil) {
        self.blockedCount = blockedCount
        self.lastCategory = lastCategory
        self.lastAt = lastAt
    }
}

public struct ProtectionAuditReader: Sendable {
    public let url: URL

    public init(url: URL = ProtectionAuditReader.defaultURL()) {
        self.url = url
    }

    public static func defaultURL() -> URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Agent Runtime", isDirectory: true)
            .appendingPathComponent("protected-attempts.json", isDirectory: false)
    }

    public func read() -> ProtectionAuditSnapshot {
        guard let data = try? Data(contentsOf: url),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return ProtectionAuditSnapshot()
        }
        let count = object["blocked_count"] as? Int ?? 0
        let events = object["events"] as? [[String: Any]] ?? []
        let last = events.last
        return ProtectionAuditSnapshot(
            blockedCount: count,
            lastCategory: last?["category"] as? String,
            lastAt: last?["at"] as? String
        )
    }
}
