import SwiftUI
import VisionKit
import Vision

struct DocumentScanner: UIViewControllerRepresentable {
    var onScan: ([UIImage]) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(onScan: onScan) }

    func makeUIViewController(context: Context) -> VNDocumentCameraViewController {
        let vc = VNDocumentCameraViewController()
        vc.delegate = context.coordinator
        return vc
    }

    func updateUIViewController(_ uiViewController: VNDocumentCameraViewController, context: Context) {}

    final class Coordinator: NSObject, VNDocumentCameraViewControllerDelegate {
        let onScan: ([UIImage]) -> Void
        init(onScan: @escaping ([UIImage]) -> Void) { self.onScan = onScan }
        func documentCameraViewController(_ controller: VNDocumentCameraViewController, didFinishWith scan: VNDocumentCameraScan) {
            var images: [UIImage] = []
            for i in 0..<scan.pageCount { images.append(scan.imageOfPage(at: i)) }
            controller.dismiss(animated: true) { self.onScan(images) }
        }
        func documentCameraViewControllerDidCancel(_ controller: VNDocumentCameraViewController) {
            controller.dismiss(animated: true)
        }
        func documentCameraViewController(_ controller: VNDocumentCameraViewController, didFailWithError error: Error) {
            controller.dismiss(animated: true)
        }
    }
}

enum PDFComposer {
    static func pdfFrom(images: [UIImage]) throws -> Data {
        let pageRect = CGRect(x: 0, y: 0, width: 612, height: 792) // US Letter @72dpi
        let fmt = UIGraphicsPDFRendererFormat()
        let rnd = UIGraphicsPDFRenderer(bounds: pageRect, format: fmt)
        return rnd.pdfData { ctx in
            for img in images {
                ctx.beginPage()
                let aspect = min(pageRect.width / img.size.width, pageRect.height / img.size.height)
                let size = CGSize(width: img.size.width * aspect, height: img.size.height * aspect)
                let origin = CGPoint(x: (pageRect.width - size.width)/2, y: (pageRect.height - size.height)/2)
                img.draw(in: CGRect(origin: origin, size: size))
            }
        }
    }
}

