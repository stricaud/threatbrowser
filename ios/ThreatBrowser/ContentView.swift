import SwiftUI

struct ContentView: View {
    @EnvironmentObject var state: AppState
    @State private var selectedArticle: Article?
    @State private var showSettings = false
    @State private var showAddSource = false
    @State private var columnVisibility = NavigationSplitViewVisibility.all

    var body: some View {
        NavigationSplitView(columnVisibility: $columnVisibility) {
            SourcesSidebar(showAddSource: $showAddSource)
        } content: {
            ArticleListView(selectedArticle: $selectedArticle)
        } detail: {
            if let article = selectedArticle {
                ArticleDetailView(article: article)
            } else {
                ContentUnavailableView(
                    "No article selected",
                    systemImage: "doc.text.magnifyingglass",
                    description: Text("Select an article from the list")
                )
                .foregroundStyle(.secondary)
            }
        }
        .navigationSplitViewStyle(.balanced)
        .toolbar {
            ToolbarItem(placement: .navigationBarTrailing) {
                Button { showSettings = true } label: {
                    Image(systemName: "gear")
                }
            }
        }
        .sheet(isPresented: $showSettings) { SettingsView() }
        .sheet(isPresented: $showAddSource) {
            AddSourceView { await state.loadSources() }
        }
    }
}
