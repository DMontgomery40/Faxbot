import Foundation
import UserNotifications
import UIKit

final class NotificationManager: NSObject, UNUserNotificationCenterDelegate {
    static let shared = NotificationManager()
    private override init() { super.init() }

    struct Prefs {
        static var allow: Bool { UserDefaults.standard.bool(forKey: "notif_allow") }
        static var outboundSuccess: Bool { UserDefaults.standard.bool(forKey: "notif_out_success") }
        static var outboundFailure: Bool { UserDefaults.standard.bool(forKey: "notif_out_failure") }
        static var inbound: Bool { UserDefaults.standard.bool(forKey: "notif_inbound") }
    }

    func configure() {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        if Prefs.allow {
            center.requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
        }
    }

    func requestAuthorization() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { granted, _ in
            DispatchQueue.main.async { UserDefaults.standard.set(granted, forKey: "notif_allow") }
        }
    }

    func notifyOutbound(jobId: String, status: String, to: String?) {
        guard Prefs.allow else { return }
        let upper = status.uppercased()
        if (upper.contains("SUCCESS") && Prefs.outboundSuccess) || (upper.contains("FAIL") && Prefs.outboundFailure) {
            let content = UNMutableNotificationContent()
            if upper.contains("SUCCESS") {
                content.title = "Fax sent"
                content.body = "Your fax to \(to ?? "") was delivered."
            } else {
                content.title = "Fax failed"
                content.body = "We couldn't complete your fax to \(to ?? ""). Tap for help."
            }
            content.sound = .default
            let req = UNNotificationRequest(identifier: "fax_\(jobId)", content: content, trigger: nil)
            UNUserNotificationCenter.current().add(req)
        }
    }

    func notifyInbound(id: String, from: String?) {
        guard Prefs.allow && Prefs.inbound else { return }
        let content = UNMutableNotificationContent()
        content.title = "New fax received"
        content.body = from != nil ? "From \(from!)" : "Open to view"
        content.sound = .default
        let req = UNNotificationRequest(identifier: "inbound_\(id)", content: content, trigger: nil)
        UNUserNotificationCenter.current().add(req)
    }

    // MARK: UNUserNotificationCenterDelegate
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification, withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.sound, .banner, .list])
    }
}

