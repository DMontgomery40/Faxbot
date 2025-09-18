import SwiftUI
import UniformTypeIdentifiers

struct DocumentPicker: UIViewControllerRepresentable {
    var onPick: (URL?, Data?) -> Void
    func makeUIViewController(context: Context) -> UIDocumentPickerViewController {
        let ctrl = UIDocumentPickerViewController(forOpeningContentTypes: [UTType.pdf], asCopy: true)
        ctrl.allowsMultipleSelection = false
        ctrl.delegate = context.coordinator
        return ctrl
    }
    func updateUIViewController(_ uiViewController: UIDocumentPickerViewController, context: Context) {}
    func makeCoordinator() -> Coordinator { Coordinator(onPick: onPick) }
    final class Coordinator: NSObject, UIDocumentPickerDelegate {
        let onPick: (URL?, Data?) -> Void
        init(onPick: @escaping (URL?, Data?) -> Void) { self.onPick = onPick }
        func documentPicker(_ controller: UIDocumentPickerViewController, didPickDocumentsAt urls: [URL]) {
            guard let url = urls.first else { onPick(nil, nil); return }
            let data = try? Data(contentsOf: url)
            onPick(url, data)
        }
        func documentPickerWasCancelled(_ controller: UIDocumentPickerViewController) { onPick(nil, nil) }
    }
}

