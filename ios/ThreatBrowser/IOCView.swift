import SwiftUI

struct IOCView: View {
    let articleUUID: String
    let articleTitle: String

    @State private var iocs: IOCResult?
    @State private var isLoading = false
    @State private var applyWL = true
    @State private var copied: String?

    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationView {
            Group {
                if isLoading {
                    ProgressView("Extracting IOCs…")
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else if let iocs {
                    if iocs.isEmpty {
                        ContentUnavailableView(
                            "No IOCs found",
                            systemImage: "shield.slash",
                            description: Text(iocs.filtered_count > 0
                                ? "\(iocs.filtered_count) removed by filters"
                                : "No indicators detected in this article")
                        )
                    } else {
                        List {
                            if iocs.filtered_count > 0 {
                                Section {
                                    Text("⚡ \(iocs.filtered_count) indicator\(iocs.filtered_count == 1 ? "" : "s") removed by warning lists and custom rules")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                            }
                            IOCSection(title: "CVEs",         items: iocs.cves,    color: .red)
                            IOCSection(title: "IP Addresses", items: iocs.ips,     color: .orange)
                            IOCSection(title: "Domains",      items: iocs.domains, color: .blue)
                            IOCSection(title: "SHA-256",      items: iocs.sha256,  color: .green)
                            IOCSection(title: "SHA-1",        items: iocs.sha1,    color: .green)
                            IOCSection(title: "MD5",          items: iocs.md5,     color: .green)
                            IOCSection(title: "URLs",         items: iocs.urls,    color: .secondary)
                        }
                        .listStyle(.insetGrouped)
                    }
                } else {
                    Color.clear
                }
            }
            .navigationTitle("IOCs")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .navigationBarLeading) {
                    Button("Done") { dismiss() }
                }
                ToolbarItem(placement: .navigationBarTrailing) {
                    Toggle(isOn: $applyWL) {
                        Text("MISP WL").font(.caption)
                    }
                    .toggleStyle(.button)
                    .onChange(of: applyWL) { _, _ in Task { await load() } }
                }
            }
            .task { await load() }
        }
    }

    private func load() async {
        isLoading = true
        defer { isLoading = false }
        iocs = try? await APIClient.shared.getIOCs(articleUUID, applyWL: applyWL)
    }
}

private struct IOCSection: View {
    let title: String
    let items: [String]
    let color: Color

    var body: some View {
        if !items.isEmpty {
            Section {
                ForEach(items, id: \.self) { item in
                    IOCRow(value: item, color: color)
                }
            } header: {
                HStack {
                    Text(title).textCase(.uppercase)
                    Spacer()
                    Text("\(items.count)").foregroundStyle(.secondary)
                }
            }
        }
    }
}

private struct IOCRow: View {
    let value: String
    let color: Color
    @State private var didCopy = false

    var body: some View {
        HStack {
            Text(value)
                .font(.system(.caption, design: .monospaced))
                .foregroundStyle(color)
                .lineLimit(2)
                .minimumScaleFactor(0.7)

            Spacer()

            Button {
                UIPasteboard.general.string = value
                didCopy = true
                Task {
                    try? await Task.sleep(for: .seconds(1.5))
                    didCopy = false
                }
            } label: {
                Image(systemName: didCopy ? "checkmark" : "doc.on.doc")
                    .foregroundStyle(didCopy ? .green : .secondary)
                    .font(.caption)
            }
            .buttonStyle(.plain)
        }
    }
}
