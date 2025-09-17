import Foundation
import SwiftUI

@MainActor
final class FaxbotClient: ObservableObject {
    @Published var serverConfig = ServerConfig()
    @Published var isConnected = false
    @Published private(set) var apiKeyMasked: String = ""

    private let session = URLSession(configuration: .default)

    struct InboundItem: Identifiable, Decodable {
        let id: String
        let fr: String?
        let to: String?
        let status: String
        let backend: String
        let pages: Int?
        let received_at: String?
    }

    func bootstrapFromKeychain() async {
        do {
            try await KeychainHelper.shared.load(into: &serverConfig)
            maskKey()
        } catch {
            // ignore
        }
    }

    func persistToKeychain() async {
        do {
            try await KeychainHelper.shared.save(from: serverConfig)
            maskKey()
        } catch {
            // ignore for now
        }
    }

    private func maskKey() {
        let k = serverConfig.apiKey
        if k.isEmpty { apiKeyMasked = ""; return }
        let last = k.suffix(4)
        apiKeyMasked = String(repeating: "•", count: max(0, k.count - 4)) + last
    }

    func redeemPairing(code: String) async throws {
        guard let url = URL(string: serverConfig.bestBaseURL + "/mobile/pair") else { throw URLError(.badURL) }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let body = ["code": code, "device_name": UIDevice.current.name]
        req.httpBody = try JSONSerialization.data(withJSONObject: body, options: [])
        let (data, resp) = try await session.data(for: req)
        guard let http = resp as? HTTPURLResponse else { throw URLError(.badServerResponse) }
        guard http.statusCode == 200 else { throw NSError(domain: "Pairing", code: http.statusCode) }
        struct PairOut: Decodable { let base_urls: [String:String?]; let token: String }
        let po = try JSONDecoder().decode(PairOut.self, from: data)
        // Update config
        if let local = po.base_urls["local"] ?? nil { serverConfig.localURL = local ?? "" }
        if let tun = po.base_urls["tunnel"] ?? nil { serverConfig.tunnelURL = tun ?? "" }
        if let pub = po.base_urls["public"] ?? nil, serverConfig.tunnelURL.isEmpty { serverConfig.tunnelURL = pub ?? "" }
        serverConfig.apiKey = po.token
        try await persistToKeychain()
        // Also save to app group for share extension
        let suite = UserDefaults(suiteName: "group.net.faxbot.shared")
        suite?.set(serverConfig.localURL, forKey: "localURL")
        suite?.set(serverConfig.tunnelURL, forKey: "tunnelURL")
        suite?.set(serverConfig.apiKey, forKey: "apiKey")
    }

    func testHealth() async -> Bool {
        let candidates = serverConfig.candidateURLs
        for base in candidates {
            if await healthOK(base) { return true }
        }
        return false
    }

    private func healthOK(_ base: String) async -> Bool {
        guard let url = URL(string: base + "/health") else { return false }
        var req = URLRequest(url: url)
        req.setValue(serverConfig.apiKey, forHTTPHeaderField: "X-API-Key")
        do {
            let (_, resp) = try await session.data(for: req)
            return (resp as? HTTPURLResponse)?.statusCode == 200
        } catch { return false }
    }

    // MARK: - Fax

    enum FaxError: Error { case invalidNumber, fileTooLarge, unsupportedType, server(Int) }

    func sendFax(to number: String, file data: Data, filename: String) async throws -> String {
        // Validate number (basic)
        let e164 = number.trimmingCharacters(in: .whitespacesAndNewlines)
        guard e164.range(of: "^\\+?[0-9]{6,20}$", options: .regularExpression) != nil else {
            throw FaxError.invalidNumber
        }
        // Size check 10 MB
        if data.count > 10 * 1024 * 1024 { throw FaxError.fileTooLarge }
        // Determine content type by extension
        let ct: String
        if filename.lowercased().hasSuffix(".pdf") { ct = "application/pdf" }
        else if filename.lowercased().hasSuffix(".txt") { ct = "text/plain" }
        else { throw FaxError.unsupportedType }
        guard let url = URL(string: serverConfig.bestBaseURL + "/fax") else { throw URLError(.badURL) }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue(serverConfig.apiKey, forHTTPHeaderField: "X-API-Key")
        // Multipart
        let boundary = "Boundary-" + UUID().uuidString
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        var body = Data()
        func append(_ s: String) { body.append(s.data(using: .utf8)!) }
        append("--\(boundary)\r\n")
        append("Content-Disposition: form-data; name=\"to\"\r\n\r\n\(e164)\r\n")
        append("--\(boundary)\r\n")
        append("Content-Disposition: form-data; name=\"file\"; filename=\"\(filename)\"\r\n")
        append("Content-Type: \(ct)\r\n\r\n")
        body.append(data)
        append("\r\n--\(boundary)--\r\n")
        req.httpBody = body
        let (respData, resp) = try await session.data(for: req)
        guard let http = resp as? HTTPURLResponse else { throw URLError(.badServerResponse) }
        if !(200..<300).contains(http.statusCode) {
            switch http.statusCode {
            case 400: throw FaxError.invalidNumber
            case 401: throw NSError(domain: "Auth", code: 401, userInfo: [NSLocalizedDescriptionKey: "Invalid API key or insufficient permissions"])
            case 413: throw FaxError.fileTooLarge
            case 415: throw FaxError.unsupportedType
            default: throw FaxError.server(http.statusCode)
            }
        }
        // Return job id if present
        if let json = try? JSONSerialization.jsonObject(with: respData) as? [String:Any], let jid = json["id"] as? String {
            // Save to local history
            await FaxHistory.shared.append(jobId: jid, to: e164, filename: filename, status: (json["status"] as? String) ?? "queued")
            return jid
        }
        return ""
    }

    func sendFax(toMany numbers: [String], file data: Data, filename: String) async -> [(to: String, result: Result<String, Error>)] {
        var out: [(String, Result<String, Error>)] = []
        for n in numbers {
            do {
                let jid = try await sendFax(to: n, file: data, filename: filename)
                out.append((n, .success(jid)))
            } catch { out.append((n, .failure(error))) }
        }
        return out
    }

    func getFaxStatus(jobId: String) async throws -> FaxJob {
        guard let url = URL(string: serverConfig.bestBaseURL + "/fax/\(jobId)") else { throw URLError(.badURL) }
        var req = URLRequest(url: url)
        req.setValue(serverConfig.apiKey, forHTTPHeaderField: "X-API-Key")
        let (data, resp) = try await session.data(for: req)
        guard (resp as? HTTPURLResponse)?.statusCode == 200 else { throw URLError(.badServerResponse) }
        return try JSONDecoder().decode(FaxJob.self, from: data)
    }

    // MARK: - Inbound

    func listInbound() async throws -> [InboundItem] {
        guard let url = URL(string: serverConfig.bestBaseURL + "/inbound") else { return [] }
        var req = URLRequest(url: url)
        req.setValue(serverConfig.apiKey, forHTTPHeaderField: "X-API-Key")
        let (data, resp) = try await session.data(for: req)
        guard (resp as? HTTPURLResponse)?.statusCode == 200 else { return [] }
        return (try? JSONDecoder().decode([InboundItem].self, from: data)) ?? []
    }

    func downloadInboundPdf(id: String) async throws -> URL {
        guard let url = URL(string: serverConfig.bestBaseURL + "/inbound/\(id)/pdf") else { throw URLError(.badURL) }
        var req = URLRequest(url: url)
        req.setValue(serverConfig.apiKey, forHTTPHeaderField: "X-API-Key")
        let (data, resp) = try await session.data(for: req)
        guard (resp as? HTTPURLResponse)?.statusCode == 200 else { throw URLError(.badServerResponse) }
        let tmp = FileManager.default.temporaryDirectory.appendingPathComponent("inbound_\(id).pdf")
        try? FileManager.default.removeItem(at: tmp)
        try data.write(to: tmp)
        return tmp
    }
}

struct ServerConfig: Codable {
    var localURL: String = ""      // e.g., http://192.168.1.100:8080
    var tunnelURL: String = ""     // e.g., https://my-faxbot.example.com
    var apiKey: String = ""

    var bestBaseURL: String {
        if isOnLAN, !localURL.isEmpty { return localURL }
        if !tunnelURL.isEmpty { return tunnelURL }
        return localURL.isEmpty ? tunnelURL : localURL
    }
    var isOnLAN: Bool { true } // TODO: detect LAN per plan
    var candidateURLs: [String] {
        var arr: [String] = []
        if !localURL.isEmpty { arr.append(localURL) }
        if !tunnelURL.isEmpty { arr.append(tunnelURL) }
        return arr
    }
}

actor FaxHistory {
    static let shared = FaxHistory()
    private let key = "fax_history"
    private init() {}
    func append(jobId: String, to: String, filename: String, status: String = "queued") {
        var arr = (UserDefaults.standard.array(forKey: key) as? [[String:String]]) ?? []
        arr.insert(["id": jobId, "to": to, "file": filename, "ts": ISO8601DateFormatter().string(from: Date()), "status": status], at: 0)
        UserDefaults.standard.set(arr, forKey: key)
    }
    func update(jobId: String, status: String?, error: String?) {
        var arr = (UserDefaults.standard.array(forKey: key) as? [[String:String]]) ?? []
        if let idx = arr.firstIndex(where: { $0["id"] == jobId }) {
            var item = arr[idx]
            if let s = status { item["status"] = s }
            if let e = error { item["err"] = e }
            arr[idx] = item
            UserDefaults.standard.set(arr, forKey: key)
        }
    }
    func list() -> [[String:String]] { (UserDefaults.standard.array(forKey: key) as? [[String:String]]) ?? [] }
}

actor RecentContacts {
    static let shared = RecentContacts()
    private let key = "fax_recent_contacts"
    private init() {}
    func add(number: String) {
    struct FaxJob: Decodable {
        let id: String
        let status: String
        let backend: String?
        let pages: Int?
        let error: String?
        let created_at: String?
        let updated_at: String?
    }

        var arr = (UserDefaults.standard.array(forKey: key) as? [String]) ?? []
        if let idx = arr.firstIndex(of: number) { arr.remove(at: idx) }
        arr.insert(number, at: 0)
        if arr.count > 20 { arr.removeLast(arr.count - 20) }
        UserDefaults.standard.set(arr, forKey: key)
    }
    func list() -> [String] { (UserDefaults.standard.array(forKey: key) as? [String]) ?? [] }
}

actor KeychainHelper {
    static let shared = KeychainHelper()
    private init() {}

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
        // Placeholder; replace with real Keychain API in implementation
        UserDefaults.standard.set(value, forKey: "fb_" + key)
    }
    private func get(key: String) async throws -> String? {
        UserDefaults.standard.string(forKey: "fb_" + key)
    }
}
