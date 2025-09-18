import SwiftUI

struct ToastView: View {
    let text: String
    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "paperplane.fill").font(.caption)
            Text(text).font(.footnote.weight(.semibold))
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
        .background(.ultraThinMaterial, in: Capsule())
        .overlay(Capsule().stroke(Color.white.opacity(0.15), lineWidth: 1))
        .shadow(color: .black.opacity(0.25), radius: 10, x: 0, y: 8)
    }
}

