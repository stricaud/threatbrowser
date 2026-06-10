import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var state: AppState
    @Environment(\.dismiss) private var dismiss

    @State private var serverURL = APIClient.shared.baseURL
    @State private var isTestingConnection = false
    @State private var connectionStatus: ConnectionStatus?

    enum ConnectionStatus { case ok(String), error(String) }

    var body: some View {
        NavigationView {
            Form {
                // Server
                Section {
                    HStack {
                        TextField("http://hostname:7474", text: $serverURL)
                            .keyboardType(.URL)
                            .autocorrectionDisabled()
                            .textInputAutocapitalization(.never)
                        Button {
                            Task { await testConnection() }
                        } label: {
                            if isTestingConnection {
                                ProgressView().controlSize(.small)
                            } else {
                                Image(systemName: "network")
                                    .foregroundStyle(statusColor)
                            }
                        }
                    }
                } header: {
                    Text("Server URL")
                } footer: {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("The address of your ThreatBrowser backend. Default: http://localhost:7474")
                        if let s = connectionStatus {
                            switch s {
                            case .ok(let m):    Text("✓ \(m)").foregroundStyle(.green)
                            case .error(let m): Text("✗ \(m)").foregroundStyle(.red)
                            }
                        }
                    }
                    .font(.footnote)
                }

                // Stats
                if let s = state.stats {
                    Section("Database") {
                        StatRow(label: "Total articles", value: s.total.formatted())
                        if let n = s.new,  n > 0 { StatRow(label: "New",  value: n.formatted(), color: .blue) }
                        if let v = s.seen, v > 0 { StatRow(label: "Seen", value: v.formatted()) }
                    }
                }

                // Sources summary
                Section("Sources") {
                    StatRow(label: "Total sources",  value: state.sources.count.formatted())
                    StatRow(label: "Active sources", value: state.sources.filter(\.active).count.formatted(), color: .green)
                }

                // Actions
                Section {
                    Button {
                        Task {
                            await state.startFetch()
                            dismiss()
                        }
                    } label: {
                        Label("Fetch all sources", systemImage: "arrow.clockwise")
                    }
                    .disabled(state.isFetching)
                }
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .navigationBarLeading) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button("Save") {
                        APIClient.shared.baseURL = serverURL.trimmingCharacters(in: .whitespacesAndNewlines)
                        Task { await state.loadAll() }
                        dismiss()
                    }
                }
            }
            .task { await state.loadStats() }
        }
    }

    private var statusColor: Color {
        switch connectionStatus {
        case .ok:    return .green
        case .error: return .red
        case nil:    return .secondary
        }
    }

    private func testConnection() async {
        isTestingConnection = true
        connectionStatus = nil
        let saved = APIClient.shared.baseURL
        APIClient.shared.baseURL = serverURL.trimmingCharacters(in: .whitespacesAndNewlines)
        do {
            let stats = try await APIClient.shared.getStats()
            connectionStatus = .ok("\(stats.total.formatted()) articles in database")
        } catch {
            connectionStatus = .error(error.localizedDescription)
        }
        APIClient.shared.baseURL = saved
        isTestingConnection = false
    }
}

private struct StatRow: View {
    let label: String
    let value: String
    var color: Color = .primary

    var body: some View {
        HStack {
            Text(label)
            Spacer()
            Text(value).foregroundStyle(color).fontWeight(.medium)
        }
    }
}
