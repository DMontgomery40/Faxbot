import Foundation
import Security

actor KeychainHelper {
    static let shared = KeychainHelper()
    private init() {}

    private let service = "net.faxbot.ios"

    func save(from cfg: ServerConfig) async throws {
        try await set(key: "localURL", value: cfg.localURL)
        try await set(key: "tunnelURL", value: cfg.tunnelURL)
        try await set(key: "apiKey", value: cfg.apiKey)
    }

    func load(into cfg: inout ServerConfig) async throws {
        cfg.localURL = (try? await get(key: "localURL")) ?? cfg.localURL
        cfg.tunnelURL = (try? await get(key: "tunnelURL")) ?? cfg.tunnelURL
        cfg.apiKey = (try? await get(key: "apiKey")) ?? cfg.apiKey
    }

    private func set(key: String, value: String) async throws {
        let data = value.data(using: .utf8) ?? Data()
        // Delete existing
        let q: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key
        ]
        SecItemDelete(q as CFDictionary)
        // Add new
        var attrs = q
        attrs[kSecValueData as String] = data
        let status = SecItemAdd(attrs as CFDictionary, nil)
        guard status == errSecSuccess else {
            throw NSError(domain: NSOSStatusErrorDomain, code: Int(status))
        }
    }

    private func get(key: String) async throws -> String? {
        let q: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne
        ]
        var item: CFTypeRef?
        let status = SecItemCopyMatching(q as CFDictionary, &item)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = item as? Data else {
            throw NSError(domain: NSOSStatusErrorDomain, code: Int(status))
        }
        return String(data: data, encoding: .utf8)
    }
}

