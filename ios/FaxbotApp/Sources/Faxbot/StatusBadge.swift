import SwiftUI

struct StatusBadge: View {
    let status: String
    var body: some View {
        let s = status.uppercased()
        let (text, color, icon): (String, Color, String) = {
            if s.contains("SUCCESS") || s == "SUCCESS" { return ("Sent", .green, "checkmark.seal.fill") }
            if s.contains("FAIL") || s == "FAILED" { return ("Failed", .red, "xmark.seal.fill") }
            if s.contains("PROGRESS") { return ("Sending", .orange, "hourglass") }
            return ("Queued", .blue, "clock.fill")
        }()
        HStack(spacing: 6) {
            Image(systemName: icon)
                .foregroundColor(.white)
            Text(text)
                .foregroundColor(.white)
        }
        .font(.caption2.weight(.semibold))
        .padding(.horizontal, 8).padding(.vertical, 4)
        .background(
            Capsule()
                .fill(LinearGradient(colors: [color.opacity(0.18), color.opacity(0.08)], startPoint: .topLeading, endPoint: .bottomTrailing))
        )
        .overlay(Capsule().stroke(Color.white.opacity(0.4), lineWidth: 0.5))
    }
}

