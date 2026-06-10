import SwiftUI

struct ArticleDetailView: View {
    @EnvironmentObject var state: AppState
    let article: Article

    @State private var content: ArticleContent?
    @State private var isLoading = false
    @State private var errorMsg: String?
    @State private var showIOC = false

    var body: some View {
        Group {
            if isLoading {
                ProgressView("Downloading…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let error = errorMsg {
                ContentUnavailableView("Download failed", systemImage: "xmark.circle",
                                       description: Text(error))
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let c = content {
                MarkdownWebView(markdown: c.markdown, rule: c.rule,
                                baseURL: URL(string: article.url))
                    .ignoresSafeArea(edges: .bottom)
            } else {
                Color.clear
            }
        }
        .navigationTitle(article.displayTitle)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItemGroup(placement: .navigationBarTrailing) {
                if content != nil {
                    Button { showIOC = true } label: {
                        Image(systemName: "flask")
                    }
                }
                Button {
                    Task { await load(force: true) }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                Link(destination: URL(string: article.url)!) {
                    Image(systemName: "safari")
                }
            }
        }
        .sheet(isPresented: $showIOC) {
            IOCView(articleUUID: article.uuid, articleTitle: article.displayTitle)
        }
        .task(id: article.uuid) { await load() }
    }

    private func load(force: Bool = false) async {
        isLoading = true; errorMsg = nil
        do {
            content = try await APIClient.shared.getArticleContent(article.uuid, force: force)
            // Mark as seen if it was new
            if article.status == "new" {
                await state.markArticles([article.uuid], status: "seen")
            }
        } catch {
            errorMsg = error.localizedDescription
        }
        isLoading = false
    }
}
