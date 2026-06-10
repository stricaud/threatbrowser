import Foundation

/// Persists all app data to the local filesystem so the app works without a server connection.
final class LocalStore {
    static let shared = LocalStore()
    private init() {
        try? FileManager.default.createDirectory(at: base,     withIntermediateDirectories: true)
        try? FileManager.default.createDirectory(at: mdDir,    withIntermediateDirectories: true)
    }

    // MARK: - Paths

    private let base: URL = {
        let lib = FileManager.default.urls(for: .libraryDirectory, in: .userDomainMask)[0]
        return lib.appendingPathComponent("ThreatBrowser", isDirectory: true)
    }()

    private var mdDir: URL { base.appendingPathComponent("markdown", isDirectory: true) }
    private var sourcesURL: URL { base.appendingPathComponent("sources.json") }
    private var articlesURL: URL { base.appendingPathComponent("articles.json") }
    private var statsURL:    URL { base.appendingPathComponent("stats.json") }

    // MARK: - Sources

    func saveSources(_ sources: [Source]) { encode(sources, to: sourcesURL) }
    func loadSources() -> [Source] { decode(from: sourcesURL) ?? [] }

    // MARK: - Articles

    private struct ArticleCache: Codable {
        let articles: [Article]
        let total: Int
    }

    func saveArticles(_ articles: [Article], total: Int) {
        encode(ArticleCache(articles: articles, total: total), to: articlesURL)
    }

    func loadArticles() -> (articles: [Article], total: Int) {
        let c: ArticleCache? = decode(from: articlesURL)
        return (c?.articles ?? [], c?.total ?? 0)
    }

    // MARK: - Stats

    func saveStats(_ stats: Stats) { encode(stats, to: statsURL) }
    func loadStats() -> Stats? { decode(from: statsURL) }

    // MARK: - Article markdown

    func saveMarkdown(_ md: String, for uuid: String) {
        let url = mdDir.appendingPathComponent("\(uuid).md")
        try? md.write(to: url, atomically: true, encoding: .utf8)
    }

    func loadMarkdown(for uuid: String) -> String? {
        let url = mdDir.appendingPathComponent("\(uuid).md")
        return try? String(contentsOf: url, encoding: .utf8)
    }

    func hasMarkdown(for uuid: String) -> Bool {
        FileManager.default.fileExists(atPath: mdDir.appendingPathComponent("\(uuid).md").path)
    }

    // MARK: - Helpers

    private func encode<T: Encodable>(_ value: T, to url: URL) {
        try? JSONEncoder().encode(value).write(to: url, options: .atomic)
    }

    private func decode<T: Decodable>(from url: URL) -> T? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(T.self, from: data)
    }
}
