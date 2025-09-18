import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var client: FaxbotClient
    @Environment(\.openURL) private var openURL
    @State private var localURL: String = ""
    @State private var tunnelURL: String = ""
    @State private var apiKeyMasked: String = ""
    @State private var apiKeyInput: String = ""
    @State private var pairingCode: String = ""
    @State private var testing = false
    @State private var redeeming = false
    @State private var showSimAlert = false
    @State private var infoAlert: (title: String, message: String)?

    var body: some View {
        NavigationStack {
            Form {
                Section("Server Configuration") {
                    TextField("Local URL (e.g., http://192.168.1.100:8080)", text: $localURL)
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                        .foregroundColor(.brandText)
                    TextField("Tunnel/Public URL (https://…)", text: $tunnelURL)
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                        .foregroundColor(.brandText)
                    SecureField("API Key (fbk_live_…)", text: $apiKeyInput)
                        .textInputAutocapitalization(.never)
                        .disableAutocorrection(true)
                        .foregroundColor(.brandText)
                    Button("Save") {
                        client.serverConfig.localURL = localURL
                        client.serverConfig.tunnelURL = tunnelURL
                        client.serverConfig.apiKey = apiKeyInput
                        Task { await client.persistToKeychain(); Haptics.success() }
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(.brandPrimary)
                }
                Section("Pairing") {
                    TextField("Enter pairing code", text: $pairingCode)
                        .keyboardType(.numberPad)
                        .submitLabel(.go)
                        .foregroundColor(.brandText)
                        .onSubmit { Task { await redeem() } }
                    Button(redeeming ? "Redeeming…" : "Redeem Code") {
                        Task { await redeem() }
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(.brandPrimary)
                    .disabled(redeeming || pairingCode.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    Button("Scan QR Code") {
                        // Present scanner
                        #if targetEnvironment(simulator)
                        showSimAlert = true
                        #else
                        let root = UIApplication.shared.connectedScenes
                            .compactMap { ($0 as? UIWindowScene)?.keyWindow }
                            .first?.rootViewController
                        let vc = ScannerVC()
                        vc.onCode = { code in
                            Task { try? await client.redeemPairing(code: code) }
                        }
                        root?.present(vc, animated: true)
                        #endif
                    }
                    .buttonStyle(.bordered)
                    .tint(.brandPrimary)
                }
                Section("Diagnostics") {
                    Button(testing ? "Testing…" : "Test Connectivity") {
                        Task {
                            testing = true
                            defer { testing = false }
                            let ok = await client.testHealth()
                            if ok {
                                Haptics.success()
                                infoAlert = ("Connectivity OK", "We can reach your Faxbot server.")
                            } else {
                                Haptics.warning()
                                infoAlert = ("Cannot reach server", "Check the URL and that the API is running.")
                            }
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
                    Button { if let url = URL(string: "https://faxbot.net") { openURL(url) } } label: {
                        Label("Faxbot website", systemImage: "safari")
                            .foregroundColor(.brandPrimary)
                    }
                    .buttonStyle(.plain)
                    Button { if let url = URL(string: "https://faxbot.net/docs") { openURL(url) } } label: {
                        Label("Docs", systemImage: "book")
                            .foregroundColor(.brandPrimary)
                    }
                    .buttonStyle(.plain)
                    Text("Faxbot makes faxing feel as simple as texting. Your data stays on your server; this app connects securely using a pairing code.")
                        .font(.footnote)
                        .foregroundColor(.brandSecondaryText)
                }
            }
            .navigationTitle("Settings")
            .toolbarBackground(.ultraThinMaterial, for: .navigationBar)
            .toolbarBackground(.visible, for: .navigationBar)
            .scrollContentBackground(.hidden)
            .background(Color.brandBackground)
            .alert("Camera not available in Simulator", isPresented: $showSimAlert) {
                Button("OK", role: .cancel) {}
            }
            .alert(infoAlert?.title ?? "", isPresented: Binding(get: { infoAlert != nil }, set: { if !$0 { infoAlert = nil } })) {
                Button("OK", role: .cancel) {}
            } message: {
                Text(infoAlert?.message ?? "")
            }
            .onAppear {
                localURL = client.serverConfig.localURL
                tunnelURL = client.serverConfig.tunnelURL
                apiKeyMasked = client.apiKeyMasked
                apiKeyInput = client.serverConfig.apiKey
            }
        }
    }

    private func redeem() async {
        guard !pairingCode.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        redeeming = true
        defer { redeeming = false }
        do {
            try await client.redeemPairing(code: pairingCode)
            localURL = client.serverConfig.localURL
            tunnelURL = client.serverConfig.tunnelURL
            apiKeyMasked = client.apiKeyMasked
            pairingCode = ""
            Haptics.success()
            infoAlert = ("Paired", "Your device is paired with the server.")
        } catch {
            Haptics.error()
            infoAlert = ("Pairing failed", error.localizedDescription)
        }
    }
}
