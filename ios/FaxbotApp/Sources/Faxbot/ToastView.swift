import SwiftUI

struct ToastView: View {
    let text: String
    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "paperplane.fill").font(.caption)
            Text(text)
                .font(.footnote.weight(.semibold))
                .foregroundColor(.primary)
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
        .background(Color.brandSurface.opacity(0.9), in: Capsule())
        .overlay(Capsule().stroke(Color.black.opacity(0.1), lineWidth: 1))
        .shadow(color: .black.opacity(0.25), radius: 10, x: 0, y: 8)
    }
}

