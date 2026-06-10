import SwiftUI

struct SourcesSidebar: View {
    @EnvironmentObject var state: AppState
    @Binding var showAddSource: Bool
    @State private var sourcesExpanded = true

    private let statuses: [(String, String, String)] = [
        ("all",      "All",      "tray.full"),
        ("new",      "New",      "circle.fill"),
        ("seen",     "Seen",     "checkmark.circle"),
        ("dl_error", "Errors",   "exclamationmark.circle"),
    ]

    private let sinces: [(String, String)] = [
        ("all", "∞"), ("24h", "24h"), ("7d", "7d"),
        ("30d", "30d"), ("6m", "6m"), ("1y", "1y"),
    ]

    var body: some View {
        List {
            // Status filter
            Section("Status") {
                ForEach(statuses, id: \.0) { value, label, icon in
                    Button {
                        state.statusFilter = value
                        Task { await state.loadArticles() }
                    } label: {
                        Label(label, systemImage: icon)
                            .foregroundStyle(state.statusFilter == value ? .accent : .primary)
                            .fontWeight(state.statusFilter == value ? .semibold : .regular)
                    }
                    .listRowBackground(
                        state.statusFilter == value ? Color.accentColor.opacity(0.15) : Color.clear
                    )
                }
            }

            // Since filter
            Section("Since") {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 6) {
                        ForEach(sinces, id: \.0) { value, label in
                            Button(label) {
                                state.sinceFilter = value
                                Task { await state.loadArticles() }
                            }
                            .font(.caption.weight(state.sinceFilter == value ? .semibold : .regular))
                            .padding(.horizontal, 10).padding(.vertical, 4)
                            .background(
                                state.sinceFilter == value
                                ? Color.accentColor.opacity(0.25)
                                : Color.secondary.opacity(0.15),
                                in: Capsule()
                            )
                            .foregroundStyle(state.sinceFilter == value ? .accent : .secondary)
                        }
                    }
                    .padding(.vertical, 2)
                }
                .listRowInsets(EdgeInsets(top: 4, leading: 12, bottom: 4, trailing: 12))
            }

            // Sources (collapsible)
            Section(isExpanded: $sourcesExpanded) {
                if state.sources.isEmpty {
                    Text("No sources").foregroundStyle(.secondary).font(.caption)
                } else {
                    ForEach(state.sources) { source in
                        SourceRow(source: source)
                    }
                }
            } header: {
                HStack {
                    Text("Sources")
                    Spacer()
                    if !state.selectedSourceUUIDs.isEmpty {
                        Button("Clear") {
                            state.selectedSourceUUIDs.removeAll()
                            Task { await state.loadArticles() }
                        }
                        .font(.caption)
                        .foregroundStyle(.accent)
                    }
                }
            }
        }
        .listStyle(.sidebar)
        .navigationTitle("ThreatBrowser")
        .toolbar {
            ToolbarItemGroup(placement: .navigationBarTrailing) {
                FetchButton()
                Button { showAddSource = true } label: {
                    Image(systemName: "plus")
                }
            }
        }
        .refreshable { await state.loadSources() }
    }
}

// MARK: - Source row

private struct SourceRow: View {
    @EnvironmentObject var state: AppState
    let source: Source
    private var isSelected: Bool { state.selectedSourceUUIDs.contains(source.uuid) }

    var body: some View {
        Button {
            state.toggleSource(source.uuid)
            Task { await state.loadArticles() }
        } label: {
            HStack(spacing: 8) {
                if let domain = source.domain {
                    AsyncImage(url: APIClient.shared.faviconURL(domain: domain)) { img in
                        img.resizable().scaledToFit()
                    } placeholder: {
                        Image(systemName: "globe").foregroundStyle(.secondary)
                    }
                    .frame(width: 14, height: 14)
                }

                VStack(alignment: .leading, spacing: 1) {
                    Text(source.name)
                        .font(.footnote)
                        .lineLimit(1)
                        .foregroundStyle(isSelected ? .accent : .primary)
                    if source.article_count > 0 {
                        Text("\(source.article_count)")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }

                Spacer()

                if !source.active {
                    Text("off").font(.caption2).foregroundStyle(.red)
                }
                if isSelected {
                    Image(systemName: "checkmark").font(.caption2).foregroundStyle(.accent)
                }
            }
        }
        .listRowBackground(
            isSelected ? Color.accentColor.opacity(0.12) : Color.clear
        )
    }
}

// MARK: - Fetch button

struct FetchButton: View {
    @EnvironmentObject var state: AppState

    var body: some View {
        Button {
            Task {
                let uuids = state.selectedSourceUUIDs.isEmpty ? nil : Array(state.selectedSourceUUIDs)
                await state.startFetch(sourceUUIDs: uuids)
            }
        } label: {
            if state.isFetching {
                HStack(spacing: 4) {
                    ProgressView().controlSize(.mini)
                    if let s = state.fetchStatus, s.total > 0 {
                        Text("\(s.done)/\(s.total)").font(.caption2)
                    }
                }
            } else {
                Image(systemName: "arrow.clockwise")
            }
        }
        .disabled(state.isFetching)
    }
}
