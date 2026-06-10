import Foundation

// MARK: - Source

struct Source: Identifiable, Hashable {
    let id: Int
    let uuid: String
    let name: String
    let url: String
    let scraper: String
    let active: Bool
    let last_fetched: String?
    let article_count: Int
    let tags: [String]

    var domain: String? { URL(string: url)?.host }

    static func == (lhs: Source, rhs: Source) -> Bool { lhs.uuid == rhs.uuid }
    func hash(into hasher: inout Hasher) { hasher.combine(uuid) }
}

extension Source: Codable {
    enum CodingKeys: String, CodingKey {
        case id, uuid, name, url, scraper, active, last_fetched, article_count, tags
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id           = try c.decode(Int.self,    forKey: .id)
        uuid         = try c.decode(String.self, forKey: .uuid)
        name         = try c.decode(String.self, forKey: .name)
        url          = try c.decode(String.self, forKey: .url)
        scraper      = try c.decode(String.self, forKey: .scraper)
        // Server may send true/false (bool) or 1/0 (int) depending on code path
        if let b = try? c.decode(Bool.self, forKey: .active) {
            active = b
        } else {
            active = (try c.decode(Int.self, forKey: .active)) != 0
        }
        last_fetched  = try c.decodeIfPresent(String.self, forKey: .last_fetched)
        article_count = try c.decode(Int.self,    forKey: .article_count)
        tags          = try c.decode([String].self, forKey: .tags)
    }
}

// MARK: - Article

struct ArticlesResponse: Codable {
    let articles: [Article]
    let total: Int
}

struct Article: Codable, Identifiable, Hashable {
    let uuid: String
    let title: String?
    let url: String
    let published_at: String?
    let first_seen: String
    let status: String
    let cached_at: String?
    let download_status: Int?
    let source_name: String
    let source_uuid: String

    var id: String { uuid }
    var displayTitle: String { title?.isEmpty == false ? title! : url }

    var displayDate: String {
        guard let date = parseISO(published_at ?? first_seen) else { return "" }
        let diff = Date().timeIntervalSince(date)
        if diff < 3600   { return "\(max(0, Int(diff / 60)))m" }
        if diff < 86400  { return "\(Int(diff / 3600))h" }
        let days = Int(diff / 86400)
        if days < 7      { return "\(days)d" }
        let f = DateFormatter()
        f.dateFormat = days < 365 ? "MMM d" : "MMM d, yy"
        return f.string(from: date)
    }

    var displayDateFull: String {
        guard let date = parseISO(published_at ?? first_seen) else { return "" }
        let f = DateFormatter(); f.dateStyle = .medium; f.timeStyle = .short
        return "\(published_at != nil ? "Published" : "Downloaded"): \(f.string(from: date))"
    }

    static func == (lhs: Article, rhs: Article) -> Bool { lhs.uuid == rhs.uuid }
    func hash(into hasher: inout Hasher) { hasher.combine(uuid) }
}

// MARK: - Article content

struct ArticleContent: Codable {
    let markdown: String
    let url: String
    let cached_at: String?
    let from_cache: Bool
    let rule: ContentRule?
}

struct ContentRule: Codable {
    let id: Int?
    let pattern: String?
    let rule_start: String?
    let rule_end: String?
    let source_level: Bool?
}

// MARK: - Fetch

struct FetchStatus: Codable {
    let running: Bool
    let total: Int
    let done: Int
    let errors: Int
    let new_articles: Int
    let timed_out: [String]
    let in_flight: [String]
}

// MARK: - Stats

struct Stats: Codable {
    let total: Int
    let new: Int?
    let seen: Int?
    let no_scenario: Int?
}

// MARK: - IOCs

struct IOCResult: Codable {
    let ips: [String]
    let domains: [String]
    let sha256: [String]
    let sha1: [String]
    let md5: [String]
    let cves: [String]
    let urls: [String]
    let filtered_count: Int
    let wl_applied: Bool

    var isEmpty: Bool {
        ips.isEmpty && domains.isEmpty && sha256.isEmpty &&
        sha1.isEmpty && md5.isEmpty && cves.isEmpty && urls.isEmpty
    }
}

// MARK: - Discover

struct DiscoverResult: Codable {
    let strategy: String
    let url: String
    let config: RawJSON
    let articles: [DiscoverArticle]
    let article_count: Int
    let name: String
    let notes: [String]
    let dates_from: String
}

struct DiscoverArticle: Codable {
    let title: String?
    let url: String
}

// RawJSON lets us round-trip arbitrary config dicts without defining every key
struct RawJSON: Codable {
    var value: Any

    init(_ value: Any = [:]) { self.value = value }

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if let v = try? c.decode([String: RawJSON].self) { value = v.mapValues { $0.value }; return }
        if let v = try? c.decode([RawJSON].self)          { value = v.map { $0.value }; return }
        if let v = try? c.decode(String.self)             { value = v; return }
        if let v = try? c.decode(Bool.self)               { value = v; return }
        if let v = try? c.decode(Int.self)                { value = v; return }
        if let v = try? c.decode(Double.self)             { value = v; return }
        value = NSNull()
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch value {
        case let d as [String: Any]: try c.encode(d.mapValues { RawJSON($0) })
        case let a as [Any]:         try c.encode(a.map { RawJSON($0) })
        case let s as String:        try c.encode(s)
        case let b as Bool:          try c.encode(b)
        case let i as Int:           try c.encode(i)
        case let f as Double:        try c.encode(f)
        default:                     try c.encodeNil()
        }
    }
}

// MARK: - Helpers

private func parseISO(_ s: String) -> Date? {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let d = f.date(from: s) { return d }
    f.formatOptions = [.withInternetDateTime]
    return f.date(from: s)
}
