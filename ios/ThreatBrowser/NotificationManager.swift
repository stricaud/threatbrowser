import UserNotifications

final class NotificationManager: NSObject {
    static let shared = NotificationManager()
    private override init() { super.init() }

    // MARK: - Permission

    func requestPermission() async -> Bool {
        let center = UNUserNotificationCenter.current()
        let settings = await center.notificationSettings()
        if settings.authorizationStatus == .authorized { return true }
        do {
            return try await center.requestAuthorization(options: [.alert, .badge, .sound])
        } catch {
            return false
        }
    }

    var isAuthorized: Bool {
        get async {
            let s = await UNUserNotificationCenter.current().notificationSettings()
            return s.authorizationStatus == .authorized
        }
    }

    // MARK: - Send

    /// Fire a local notification for newly discovered articles.
    func notifyNewArticles(count: Int, sources: [String] = []) async {
        guard await isAuthorized else { return }

        let content = UNMutableNotificationContent()
        content.title = "ThreatBrowser"
        content.sound = .default

        if count == 1 {
            content.body = "1 new threat report"
        } else {
            content.body = "\(count) new threat reports"
        }

        if !sources.isEmpty {
            let sourceList = sources.prefix(3).joined(separator: ", ")
            let suffix = sources.count > 3 ? " +\(sources.count - 3) more" : ""
            content.subtitle = sourceList + suffix
        }

        // Update badge to total new count
        content.badge = count as NSNumber

        let trigger = UNTimeIntervalNotificationTrigger(timeInterval: 0.5, repeats: false)
        let request = UNNotificationRequest(
            identifier: "new-articles-\(Date().timeIntervalSince1970)",
            content: content,
            trigger: trigger
        )

        try? await UNUserNotificationCenter.current().add(request)
    }

    /// Clear badge and delivered notifications.
    func clearBadge() {
        UNUserNotificationCenter.current().removeAllDeliveredNotifications()
        Task {
            try? await UNUserNotificationCenter.current().setBadgeCount(0)
        }
    }
}
