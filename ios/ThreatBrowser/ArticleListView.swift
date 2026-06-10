import SwiftUI

struct ArticleListView: View {
    @EnvironmentObject var state: AppState
    @Binding var selectedArticle: Article?
    @State private var selection = Set<String>()
    @State private var editMode: EditMode = .inactive

    private var hasMore: Bool { state.articles.count < state.totalArticles }

    var body: some View {
        List(state.articles, id: \.uuid, selection: $selection) { article in
            ArticleRow(article: article, isSelected: selectedArticle?.uuid == article.uuid)
                .contentShape(Rectangle())
                .onTapGesture { selectedArticle = article }
                .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                    Button {
                        Task { await state.markArticles([article.uuid],
                                                        status: article.status == "seen" ? "new" : "seen") }
                    } label: {
                        Label(article.status == "seen" ? "New" : "Seen",
                              systemImage: article.status == "seen" ? "circle.fill" : "checkmark.circle")
                    }
                    .tint(article.status == "seen" ? .blue : .green)
                }
                .listRowBackground(
                    selectedArticle?.uuid == article.uuid
                    ? Color.accentColor.opacity(0.12) : Color.clear
                )
        }
        .listStyle(.plain)
        .searchable(text: $state.searchText, prompt: "Search articles…")
        .onSubmit(of: .search) { Task { await state.loadArticles() } }
        .onChange(of: state.searchText) { old, new in
            if new.isEmpty && !old.isEmpty { Task { await state.loadArticles() } }
        }
        .navigationTitle(titleText)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItemGroup(placement: .navigationBarTrailing) {
                if !selection.isEmpty {
                    Menu {
                        Button("Mark seen") {
                            Task { await state.markArticles(Array(selection), status: "seen") }
                            selection.removeAll()
                        }
                        Button("Mark new") {
                            Task { await state.markArticles(Array(selection), status: "new") }
                            selection.removeAll()
                        }
                    } label: {
                        Image(systemName: "ellipsis.circle")
                    }
                }
                EditButton()
            }
        }
        .environment(\.editMode, $editMode)
        .overlay {
            if state.isLoadingArticles && state.articles.isEmpty {
                ProgressView("Loading…")
            } else if state.articles.isEmpty && !state.isLoadingArticles {
                ContentUnavailableView(
                    "No articles",
                    systemImage: "doc.text",
                    description: Text("Fetch sources to populate the feed")
                )
            }
        }
        .safeAreaInset(edge: .bottom) {
            if hasMore {
                Button {
                    Task { await state.loadArticles(append: true) }
                } label: {
                    HStack {
                        if state.isLoadingArticles {
                            ProgressView().controlSize(.small)
                        }
                        Text("Load more (\(state.totalArticles - state.articles.count) remaining)")
                            .font(.footnote)
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 10)
                    .background(.ultraThinMaterial)
                }
                .disabled(state.isLoadingArticles)
            }
        }
        .refreshable { await state.loadArticles() }
    }

    private var titleText: String {
        let total = state.totalArticles
        if total == 0 { return "Articles" }
        return "\(total.formatted()) article\(total == 1 ? "" : "s")"
    }
}

// MARK: - Article row

private struct ArticleRow: View {
    let article: Article
    let isSelected: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .top, spacing: 6) {
                StatusDot(status: article.status, downloadStatus: article.download_status)
                    .padding(.top, 3)

                Text(article.displayTitle)
                    .font(.footnote)
                    .foregroundStyle(article.status == "seen" ? .secondary : .primary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)

                Spacer(minLength: 4)

                Text(article.displayDate)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .fixedSize()
            }

            Text(article.source_name)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .padding(.leading, 14)
        }
        .padding(.vertical, 2)
    }
}

private struct StatusDot: View {
    let status: String
    let downloadStatus: Int?

    var body: some View {
        if let code = downloadStatus, code != 200 {
            Image(systemName: "exclamationmark.circle.fill")
                .font(.system(size: 8))
                .foregroundStyle(.red)
        } else {
            Circle()
                .fill(dotColor)
                .frame(width: 7, height: 7)
        }
    }

    private var dotColor: Color {
        switch status {
        case "new":          return .accentColor
        case "has_scenario": return .green
        default:             return Color.secondary.opacity(0.4)
        }
    }
}
