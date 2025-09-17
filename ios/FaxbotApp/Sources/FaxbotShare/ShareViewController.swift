import UIKit
import UniformTypeIdentifiers
import MobileCoreServices

final class ShareViewController: UIViewController {
    override func viewDidLoad() {
        super.viewDidLoad()
        handleShare()
    }

    private func complete() {
        self.extensionContext?.completeRequest(returningItems: [], completionHandler: nil)
    }

    private func handleShare() {
        guard let items = extensionContext?.inputItems as? [NSExtensionItem] else { return complete() }
        // Load config from App Group
        let suite = UserDefaults(suiteName: "group.net.faxbot.shared")
        let base = (suite?.string(forKey: "tunnelURL")?.isEmpty == false ? suite?.string(forKey: "tunnelURL") : suite?.string(forKey: "localURL")) ?? ""
        let token = suite?.string(forKey: "apiKey") ?? ""
        guard !base.isEmpty, !token.isEmpty else { return complete() }

        // Gather attachments
        var firstHandlerCalled = false
        for item in items {
            guard let providers = item.attachments else { continue }
            for provider in providers {
                if provider.hasItemConformingToTypeIdentifier(UTType.pdf.identifier) {
                    provider.loadItem(forTypeIdentifier: UTType.pdf.identifier, options: nil) { data, _ in
                        guard let url = data as? URL, let pdf = try? Data(contentsOf: url) else { return self.complete() }
                        self.promptForNumber { to in
                            self.upload(base: base, token: token, data: pdf, filename: url.lastPathComponent, to: to)
                        }
                    }
                    firstHandlerCalled = true; break
                } else if provider.hasItemConformingToTypeIdentifier(UTType.image.identifier) {
                    provider.loadItem(forTypeIdentifier: UTType.image.identifier, options: nil) { data, _ in
                        var image: UIImage?
                        if let url = data as? URL { image = UIImage(contentsOfFile: url.path) }
                        if image == nil, let img = data as? UIImage { image = img }
                        guard let img = image, let pdf = try? PDFComposer.pdfFrom(images: [img]) else { return self.complete() }
                        self.promptForNumber { to in
                            self.upload(base: base, token: token, data: pdf, filename: "share.pdf", to: to)
                        }
                    }
                    firstHandlerCalled = true; break
                }
            }
            if firstHandlerCalled { break }
        }
        if !firstHandlerCalled { complete() }
    }

    private func promptForNumber(_ cb: @escaping (String)->Void) {
        DispatchQueue.main.async {
            let alert = UIAlertController(title: "Send Fax", message: "Enter destination number (E.164)", preferredStyle: .alert)
            alert.addTextField { tf in tf.placeholder = "+15551234567"; tf.keyboardType = .phonePad }
            alert.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in self.complete() })
            alert.addAction(UIAlertAction(title: "Send", style: .default) { _ in
                let to = alert.textFields?.first?.text?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                guard to.range(of: "^\\+?[0-9]{6,20}$", options: .regularExpression) != nil else { return self.complete() }
                cb(to)
            })
            self.present(alert, animated: true)
        }
    }

    private func upload(base: String, token: String, data: Data, filename: String, to: String) {
        // Enforce 10 MB
        if data.count > 10 * 1024 * 1024 { return complete() }
        guard let url = URL(string: base + "/fax") else { return complete() }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue(token, forHTTPHeaderField: "X-API-Key")
        let boundary = "Boundary-" + UUID().uuidString
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        var body = Data()
        func append(_ s: String) { body.append(s.data(using: .utf8)!) }
        append("--\(boundary)\r\n")
        append("Content-Disposition: form-data; name=\"to\"\r\n\r\n\(to)\r\n")
        append("--\(boundary)\r\n")
        append("Content-Disposition: form-data; name=\"file\"; filename=\"\(filename)\"\r\n")
        append("Content-Type: application/pdf\r\n\r\n")
        body.append(data)
        append("\r\n--\(boundary)--\r\n")
        req.httpBody = body
        let task = URLSession.shared.dataTask(with: req) { _, _, _ in
            self.complete()
        }
        task.resume()
    }
}
