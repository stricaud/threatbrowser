import Foundation

final class APIClient {
    static let shared = APIClient()
    private init() {}

    var baseURL: String {
        get { UserDefaults.standard.string(forKey: "serverURL") ?? "http://localhost:7474" }
        set { UserDefaults.standard.set(newValue, forKey: "serverURL") }
    }

    // MARK: - Core request

    private func request<T: Decodable>(_ path: String, method: String = "GET", body: (any Encodable)? = nil) async throws -> T {
        let url = try makeURL(path)
        var req = URLRequest(url: url, timeoutInterval: 30)
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let body { req.httpBody = try JSONEncoder().encode(body) }
        let (data, resp) = try await URLSession.shared.data(for: req)
        if let http = resp as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
            let msg = (try? JSONDecoder().decode(APIErrorBody.self, from: data))?.detail
                   ?? String(data: data, encoding: .utf8)
                   ?? "HTTP \(http.statusCode)"
            throw APIError.server(http.statusCode, msg)
        }
        return try JSONDecoder().decode(T.self, from: data)
    }

    private func requestVoid(_ path: String, method: String, body: (any Encodable)? = nil) async throws {
        let url = try makeURL(path)
        var req = URLRequest(url: url, timeoutInterval: 30)
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let body { req.httpBody = try JSONEncoder().encode(body) }
        let (data, resp) = try await URLSession.shared.data(for: req)
        if let http = resp as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
            let msg = (try? JSONDecoder().decode(APIErrorBody.self, from: data))?.detail ?? "HTTP \(http.statusCode)"
            throw APIError.server(http.statusCode, msg)
        }
    }

    private func makeURL(_ path: String) throws -> URL {
        guard let url = URL(string: baseURL + path) else { throw APIError.badURL }
        return url
    }

    // MARK: - Sources

    func getSources() async throws -> [Source] {
        try await request("/api/sources")
    }

    func deleteSource(_ uuid: String, deleteContent: Bool = false) async throws {
        try await requestVoid("/api/sources/\(uuid)?delete_content=\(deleteContent)", method: "DELETE")
    }

    func backfillDates(_ uuid: String) async throws -> BackfillResult {
        try await request("/api/sources/\(uuid)/backfill-dates", method: "POST")
    }

    // MARK: - Articles

    func getArticles(status: String? = nil, sourceUUIDs: [String] = [],
                     q: String? = nil, since: String? = nil,
                     limit: Int = 500, offset: Int = 0,
                     dlError: Bool = false) async throws -> ArticlesResponse {
        var p: [String: String] = ["limit": "\(limit)", "offset": "\(offset)"]
        if let s = status { p["status"] = s }
        if !sourceUUIDs.isEmpty { p["source_uuids"] = sourceUUIDs.joined(separator: ",") }
        if let q, !q.isEmpty { p["q"] = q }
        if let s = since { p["since"] = s }
        if dlError { p["dl_error"] = "true" }
        let qs = p.map { "\($0)=\($1.urlEncoded)" }.joined(separator: "&")
        return try await request("/api/articles?\(qs)")
    }

    func getArticleContent(_ uuid: String, force: Bool = false) async throws -> ArticleContent {
        try await request("/api/articles/\(uuid)/content\(force ? "?force=true" : "")")
    }

    func bulkStatus(uuids: [String], status: String) async throws {
        struct Body: Encodable { let uuids: [String]; let status: String }
        try await requestVoid("/api/articles/bulk-status", method: "POST",
                              body: Body(uuids: uuids, status: status))
    }

    func getIOCs(_ uuid: String, applyWL: Bool = true) async throws -> IOCResult {
        try await request("/api/articles/\(uuid)/iocs?apply_wl=\(applyWL)")
    }

    // MARK: - Fetch

    func startFetch(sourceUUIDs: [String]? = nil) async throws {
        struct Body: Encodable { let source_uuids: [String]? }
        try await requestVoid("/api/fetch", method: "POST",
                              body: Body(source_uuids: sourceUUIDs))
    }

    func getFetchStatus() async throws -> FetchStatus {
        try await request("/api/fetch/status")
    }

    // MARK: - Stats

    func getStats() async throws -> Stats {
        try await request("/api/stats")
    }

    // MARK: - Discover & add source

    func discover(url: String) async throws -> DiscoverResult {
        struct Body: Encodable { let url: String }
        return try await request("/api/discover", method: "POST", body: Body(url: url))
    }

    func addSource(name: String, url: String, scraper: String,
                   config: RawJSON, tags: [String]) async throws -> Source {
        struct Body: Encodable {
            let name, url, scraper: String
            let config: RawJSON
            let tags: [String]
        }
        return try await request("/api/sources", method: "POST",
                                 body: Body(name: name, url: url, scraper: scraper,
                                            config: config, tags: tags))
    }

    // MARK: - Favicon URL

    func faviconURL(domain: String) -> URL? {
        URL(string: "\(baseURL)/api/favicon?domain=\(domain.urlEncoded)")
    }
}

// MARK: - Error types

enum APIError: LocalizedError {
    case badURL
    case server(Int, String)

    var errorDescription: String? {
        switch self {
        case .badURL:            return "Invalid server URL"
        case .server(let c, let m): return "Server error \(c): \(m)"
        }
    }
}

private struct APIErrorBody: Decodable { let detail: String? }

struct BackfillResult: Decodable { let pending: Int }

// MARK: - String helper

private extension String {
    var urlEncoded: String {
        addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? self
    }
}
