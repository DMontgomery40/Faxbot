import SwiftUI

@main
struct FaxbotApp: App {
    @StateObject private var client = FaxbotClient()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(client)
                .onAppear { NotificationManager.shared.configure() }
                .tint(Color.brandPrimary)
        }
    }
}
