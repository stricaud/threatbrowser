import SwiftUI

struct AddSourceView: View {
    var onAdded: (() async -> Void)?

    @Environment(\.dismiss) private var dismiss

    @State private var urlText  = ""
    @State private var step: Step = .url
    @State private var isDiscovering = false
    @State private var discoverResult: DiscoverResult?
    @State private var discoverError: String?
    @State private var sourceName = ""
    @State private var sourceTags = ""
    @State private var isSaving = false
    @State private var saveError: String?

    enum Step { case url, result }

    var body: some View {
        NavigationView {
            Form {
                switch step {
                case .url:    urlStep
                case .result: resultStep
                }
            }
            .navigationTitle(step == .url ? "Add Source" : "Review")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .navigationBarLeading) {
                    Button(step == .result ? "Back" : "Cancel") {
                        if step == .result { step = .url; discoverResult = nil }
                        else { dismiss() }
                    }
                }
                if step == .result {
                    ToolbarItem(placement: .navigationBarTrailing) {
                        Button("Add") { Task { await save() } }
                            .disabled(sourceName.trimmingCharacters(in: .whitespaces).isEmpty || isSaving)
                    }
                }
            }
        }
    }

    // MARK: - Step 1: URL

    @ViewBuilder
    private var urlStep: some View {
        Section {
            TextField("https://blog.example.com/", text: $urlText)
                .keyboardType(.URL)
                .autocorrectionDisabled()
                .textInputAutocapitalization(.never)
        } header: {
            Text("Blog or feed URL")
        } footer: {
            Text("Paste any URL — the homepage of a blog, an RSS/Atom feed, or an article listing. Discover will probe it automatically.")
        }

        if let err = discoverError {
            Section {
                Text(err).foregroundStyle(.red).font(.footnote)
            }
        }

        Section {
            Button {
                Task { await discover() }
            } label: {
                HStack {
                    if isDiscovering { ProgressView().controlSize(.small) }
                    Text(isDiscovering ? "Discovering…" : "Discover")
                        .frame(maxWidth: .infinity)
                }
            }
            .disabled(urlText.trimmingCharacters(in: .whitespaces).isEmpty || isDiscovering)
        }
    }

    // MARK: - Step 2: Result

    @ViewBuilder
    private var resultStep: some View {
        if let r = discoverResult {
            Section("Detected strategy") {
                HStack {
                    Label(
                        r.strategy == "rss" ? "RSS / Atom feed" : "HTML scraper",
                        systemImage: r.strategy == "rss" ? "antenna.radiowaves.left.and.right" : "globe"
                    )
                    Spacer()
                    if r.article_count > 0 {
                        Text("\(r.article_count) article\(r.article_count == 1 ? "" : "s")")
                            .foregroundStyle(.secondary).font(.footnote)
                    }
                }
                ForEach(r.notes, id: \.self) { note in
                    HStack(spacing: 6) {
                        Image(systemName: noteIcon(note))
                            .font(.caption)
                            .foregroundStyle(noteColor(note))
                        Text(note).font(.caption).foregroundStyle(.secondary)
                    }
                }
            }

            if !r.articles.isEmpty {
                Section("Sample articles") {
                    ForEach(r.articles.prefix(5), id: \.url) { a in
                        Text(a.title ?? a.url)
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                }
            }

            Section("Source details") {
                TextField("Name", text: $sourceName)
                TextField("Tags (comma-separated)", text: $sourceTags)
                    .autocorrectionDisabled()
                    .textInputAutocapitalization(.never)
            }

            if let err = saveError {
                Section { Text(err).foregroundStyle(.red).font(.footnote) }
            }
        }
    }

    // MARK: - Actions

    private func discover() async {
        var url = urlText.trimmingCharacters(in: .whitespaces)
        if !url.hasPrefix("http") { url = "https://" + url }
        isDiscovering = true; discoverError = nil
        do {
            let result = try await APIClient.shared.discover(url: url)
            discoverResult = result
            sourceName = result.name.isEmpty ? "" : result.name
            step = .result
        } catch {
            discoverError = error.localizedDescription
        }
        isDiscovering = false
    }

    private func save() async {
        guard let r = discoverResult else { return }
        let name = sourceName.trimmingCharacters(in: .whitespaces)
        guard !name.isEmpty else { return }
        isSaving = true; saveError = nil
        let tags = sourceTags.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        do {
            _ = try await APIClient.shared.addSource(
                name: name, url: r.url, scraper: r.strategy,
                config: r.config, tags: tags
            )
            await onAdded?()
            dismiss()
        } catch {
            saveError = error.localizedDescription
        }
        isSaving = false
    }

    private func noteIcon(_ note: String) -> String {
        let lo = note.lowercased()
        if lo.contains("detected") || lo.contains("present") || lo.contains("found") || lo.contains("rss") { return "checkmark.circle.fill" }
        if lo.contains("no ") || lo.contains("could not") || lo.contains("manual") { return "exclamationmark.triangle.fill" }
        return "info.circle"
    }

    private func noteColor(_ note: String) -> Color {
        let lo = note.lowercased()
        if lo.contains("detected") || lo.contains("present") || lo.contains("found") { return .green }
        if lo.contains("no ") || lo.contains("could not") || lo.contains("manual")   { return .orange }
        return .secondary
    }
}
