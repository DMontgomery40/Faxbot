import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var client: FaxbotClient
    @State private var localURL: String = ""
    @State private var tunnelURL: String = ""
    @State private var apiKeyMasked: String = ""
    @State private var pairingCode: String = ""
    @State private var testing = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Server Configuration") {
                    TextField("Local URL (e.g., http://192.168.1.100:8080)", text: $localURL)
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                    TextField("Tunnel/Public URL (https://…)", text: $tunnelURL)
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                    HStack {
                        Text("API Key")
                        Spacer()
                        Text(apiKeyMasked.isEmpty ? "Not set" : apiKeyMasked)
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                    Button("Save") {
                        client.serverConfig.localURL = localURL
                        client.serverConfig.tunnelURL = tunnelURL
                        Task { await client.persistToKeychain(); Haptics.success() }
                    }
                }
                Section("Pairing") {
                    TextField("Enter pairing code", text: $pairingCode)
                        .keyboardType(.numberPad)
                    Button("Redeem Code") {
                        Task {
                            do {
                                try await client.redeemPairing(code: pairingCode)
                                localURL = client.serverConfig.localURL
                                tunnelURL = client.serverConfig.tunnelURL
                                apiKeyMasked = client.apiKeyMasked
                                pairingCode = ""
                                Haptics.success()
                            } catch {
                                // Surface a gentle toast/alert in a full implementation
                                print("Pairing failed: \(error.localizedDescription)")
                                Haptics.error()
                            }
                        }
                    }
                    Button("Scan QR Code") {
                        // Present scanner
                        let root = UIApplication.shared.connectedScenes
                            .compactMap { ($0 as? UIWindowScene)?.keyWindow }
                            .first?.rootViewController
                        let vc = ScannerVC()
                        vc.onCode = { code in
                            Task { try? await client.redeemPairing(code: code) }
                        }
                        root?.present(vc, animated: true)
                    }
                }
                Section("Diagnostics") {
                    Button(testing ? "Testing…" : "Test Connectivity") {
                        Task {
                            testing = true
                            defer { testing = false }
                            let ok = await client.testHealth()
                            if ok { Haptics.success() } else { Haptics.warning() }
                        }
                    }
                    .disabled(testing)
                }
                Section("Notifications") {
                    Toggle("Allow Notifications", isOn: Binding(get: { UserDefaults.standard.bool(forKey: "notif_allow") }, set: { v in
                        UserDefaults.standard.set(v, forKey: "notif_allow")
                        if v { NotificationManager.shared.requestAuthorization() }
                    }))
                    Toggle("Notify on sent", isOn: Binding(get: { UserDefaults.standard.bool(forKey: "notif_out_success") }, set: { UserDefaults.standard.set($0, forKey: "notif_out_success") }))
                        .disabled(!UserDefaults.standard.bool(forKey: "notif_allow"))
                    Toggle("Notify on failure", isOn: Binding(get: { UserDefaults.standard.bool(forKey: "notif_out_failure") }, set: { UserDefaults.standard.set($0, forKey: "notif_out_failure") }))
                        .disabled(!UserDefaults.standard.bool(forKey: "notif_allow"))
                    Toggle("Notify on inbound", isOn: Binding(get: { UserDefaults.standard.bool(forKey: "notif_inbound") }, set: { UserDefaults.standard.set($0, forKey: "notif_inbound") }))
                        .disabled(!UserDefaults.standard.bool(forKey: "notif_allow"))
                    Button("Open System Notification Settings") {
                        if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
                    }
                }
                Section("About") {
                    Link("Faxbot website", destination: URL(string: "https://faxbot.net")!)
                    Link("Docs", destination: URL(string: "https://faxbot.net/docs")!)
                    Text("Faxbot makes faxing feel as simple as texting. Your data stays on your server; this app connects securely using a pairing code.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Settings")
            .toolbarBackground(.ultraThinMaterial, for: .navigationBar)
            .toolbarBackground(.visible, for: .navigationBar)
            .onAppear {
                localURL = client.serverConfig.localURL
                tunnelURL = client.serverConfig.tunnelURL
                apiKeyMasked = client.apiKeyMasked
            }
        }
    }
}
