import SwiftUI

@main
struct ThreatBrowserApp: App {
    @StateObject private var state = AppState()
    @State private var showSettingsFromError = false
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(state)
                .preferredColorScheme(.dark)
                .onAppear {
                    // Unstructured Task: not cancelled by iOS scene-lifecycle transitions
                    // (e.g. the Local Network permission prompt). .task {} would be cancelled
                    // and silently drop in-flight requests whenever the scene goes inactive.
                    Task {
                        await state.loadAll()
                        _ = await NotificationManager.shared.requestPermission()
                    }
                }
                .onChange(of: scenePhase) { _, phase in
                    if phase == .active {
                        NotificationManager.shared.clearBadge()
                    }
                }
                .alert("Connection Error", isPresented: Binding(
                    get: { state.errorMessage != nil },
                    set: { if !$0 { state.clearError() } }
                )) {
                    Button("Configure Server") {
                        state.clearError()
                        showSettingsFromError = true
                    }
                    Button("OK", role: .cancel) { state.clearError() }
                } message: {
                    Text(state.errorMessage ?? "")
                }
                .sheet(isPresented: $showSettingsFromError) {
                    SettingsView()
                        .environmentObject(state)
                }
        }
    }
}
