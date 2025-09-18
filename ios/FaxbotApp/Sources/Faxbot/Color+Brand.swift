import SwiftUI

extension Color {
    // Force explicit colors that work in both light and dark mode
    static var brandPrimary: Color { Color.blue }
    static var brandBackground: Color { Color(UIColor.systemBackground) }
    static var brandSurface: Color { Color(UIColor.secondarySystemBackground) }
    static var brandText: Color { Color(UIColor.label) }
    static var brandSecondaryText: Color { Color(UIColor.secondaryLabel) }
}

