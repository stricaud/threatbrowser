import Foundation
import Combine

@MainActor
final class AppState: ObservableObject {

    // MARK: - Data
    @Published var sources: [Source] = []
    @Published var articles: [Article] = []
    @Published var totalArticles: Int = 0
    @Published var stats: Stats?

    // MARK: - Filters
    @Published var selectedSourceUUIDs: Set<String> = []
    @Published var statusFilter: String = "all"   // all | new | seen | dl_error
    @Published var sinceFilter: String  = "all"   // all | 24h | 7d | 30d | 6m | 1y
    @Published var searchText: String   = ""

    // MARK: - Fetch
    @Published var isFetching: Bool = false
    @Published var fetchStatus: FetchStatus?

    // MARK: - UI
    @Published var isLoadingArticles: Bool = false
    @Published var errorMessage: String?

    private var fetchPollTask: Task<Void, Never>?
    private let api = APIClient.shared

    // MARK: - Load

    func loadAll() async {
        async let s: () = loadSources()
        async let a: () = loadArticles()
        async let st: () = loadStats()
        _ = await (s, a, st)
    }

    func loadSources() async {
        do {
            sources = try await api.getSources()
            // If articles failed to load earlier (e.g. cancelled while permission prompt
            // was shown), reload them now that the connection is known to be working.
            if articles.isEmpty { await loadArticles() }
        }
        catch { setError(error, url: api.baseURL) }
    }

    func loadArticles(append: Bool = false) async {
        isLoadingArticles = true
        defer { isLoadingArticles = false }
        do {
            let offset = append ? articles.count : 0
            let result = try await api.getArticles(
                status:      statusFilter == "all"       ? nil       : statusFilter,
                sourceUUIDs: Array(selectedSourceUUIDs),
                q:           searchText.isEmpty          ? nil       : searchText,
                since:       sinceFilter == "all"        ? nil       : sinceFilter,
                limit:       500,
                offset:      offset,
                dlError:     statusFilter == "dl_error"
            )
            if append { articles.append(contentsOf: result.articles) }
            else      { articles = result.articles }
            totalArticles = result.total
        } catch { setError(error) }
    }

    func loadStats() async {
        do { stats = try await api.getStats() }
        catch {}
    }

    // MARK: - Fetch

    func startFetch(sourceUUIDs: [String]? = nil) async {
        guard !isFetching else { return }
        do {
            try await api.startFetch(sourceUUIDs: sourceUUIDs)
            isFetching = true
            startPolling()
        } catch { setError(error) }
    }

    private func startPolling() {
        fetchPollTask?.cancel()
        fetchPollTask = Task {
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(1.5))
                guard !Task.isCancelled else { break }
                do {
                    let s = try await api.getFetchStatus()
                    fetchStatus = s
                    if !s.running {
                        isFetching = false
                        fetchPollTask?.cancel()
                        await loadArticles()
                        await loadStats()
                        await loadSources()
                        // Notify about new articles found in this fetch
                        let newCount = s.new_articles
                        if newCount > 0 {
                            await NotificationManager.shared.notifyNewArticles(count: newCount)
                        }
                        break
                    }
                } catch {}
            }
        }
    }

    // MARK: - Article actions

    func markArticles(_ uuids: [String], status: String) async {
        do {
            try await api.bulkStatus(uuids: uuids, status: status)
            for i in articles.indices where uuids.contains(articles[i].uuid) {
                articles[i] = Article(
                    uuid: articles[i].uuid, title: articles[i].title,
                    url: articles[i].url, published_at: articles[i].published_at,
                    first_seen: articles[i].first_seen, status: status,
                    cached_at: articles[i].cached_at,
                    download_status: articles[i].download_status,
                    source_name: articles[i].source_name,
                    source_uuid: articles[i].source_uuid
                )
            }
            await loadStats()
        } catch { setError(error) }
    }

    // MARK: - Filters

    func toggleSource(_ uuid: String) {
        if selectedSourceUUIDs.contains(uuid) { selectedSourceUUIDs.remove(uuid) }
        else { selectedSourceUUIDs.insert(uuid) }
    }

    // MARK: - Error

    private func setError(_ error: Error, url: String? = nil) {
        print("[ThreatBrowser] error: \(error)")
        // NSURLErrorCancelled (-999) means the Swift Task was cancelled by the system
        // (e.g. Local Network permission prompt causing scene transition). Not a user-visible error.
        if (error as? URLError)?.code == .cancelled { return }
        let isNetworkError = (error as? URLError) != nil ||
                             (error as NSError).domain == NSURLErrorDomain
        if let url, isNetworkError {
            errorMessage = "Could not reach \(url)\n\n\(error.localizedDescription)\n\nOpen Settings to change the server address."
        } else if let de = error as? DecodingError {
            errorMessage = _decodingMessage(de)
        } else {
            errorMessage = error.localizedDescription
        }
    }

    private func _decodingMessage(_ error: DecodingError) -> String {
        switch error {
        case .typeMismatch(let type, let ctx):
            let path = ctx.codingPath.map(\.stringValue).joined(separator: ".")
            return "Decode error — field \"\(path)\": expected \(type).\n\(ctx.debugDescription)"
        case .valueNotFound(let type, let ctx):
            let path = ctx.codingPath.map(\.stringValue).joined(separator: ".")
            return "Decode error — field \"\(path)\" (\(type)): value is null/missing."
        case .keyNotFound(let key, let ctx):
            let path = ctx.codingPath.map(\.stringValue).joined(separator: ".")
            return "Decode error — required field \"\(key.stringValue)\" missing at \"\(path)\"."
        case .dataCorrupted(let ctx):
            return "Decode error — data corrupted: \(ctx.debugDescription)"
        @unknown default:
            return "Decode error: \(String(describing: error))"
        }
    }

    func clearError() { errorMessage = nil }
}
