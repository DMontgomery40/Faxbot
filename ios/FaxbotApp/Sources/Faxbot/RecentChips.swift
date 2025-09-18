import SwiftUI

struct RecentChips: View {
    var onSelect: (String) -> Void
    @State private var items: [String] = []

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(items, id: \.self) { num in
                    Button(action: {
                        Haptics.lightTap()
                        onSelect(num)
                    }) {
                        HStack(spacing: 6) {
                            Image(systemName: "person.crop.circle.badge.plus")
                            Text(num)
                                .foregroundColor(.primary)
                        }
                        .font(.caption)
                        .padding(.horizontal, 10).padding(.vertical, 6)
                        .background(Capsule().fill(.thinMaterial))
                    }
                    .buttonStyle(.plain)
                    .transition(.asymmetric(insertion: .scale.combined(with: .opacity), removal: .opacity))
                }
            }
            .padding(.vertical, 2)
        }
        .onAppear { Task { items = await RecentContacts.shared.list() } }
    }
}

