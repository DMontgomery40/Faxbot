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
                        Task { await client.persistToKeychain() }
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
                            } catch {
                                // Surface a gentle toast/alert in a full implementation
                                print("Pairing failed: \(error.localizedDescription)")
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
                            _ = await client.testHealth()
                        }
                    }
                    .disabled(testing)
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
            .onAppear {
                localURL = client.serverConfig.localURL
                tunnelURL = client.serverConfig.tunnelURL
                apiKeyMasked = client.apiKeyMasked
            }
        }
    }
}
