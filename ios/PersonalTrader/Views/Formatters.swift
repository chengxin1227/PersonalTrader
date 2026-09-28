import Foundation
import SwiftUI

enum Formatters {
    static func price(_ value: Double?) -> String {
        guard let value else { return "—" }
        return value.formatted(.currency(code: "USD").precision(.fractionLength(2)))
    }

    static func percent(_ value: Double?) -> String {
        guard let value else { return "—" }
        let prefix = value > 0 ? "+" : ""
        return "\(prefix)\(value.formatted(.number.precision(.fractionLength(2))))%"
    }

    static func changeColor(_ value: Double?) -> Color {
        guard let value else { return .secondary }
        if value > 0 { return Color(red: 0.22, green: 0.78, blue: 0.45) }
        if value < 0 { return Color(red: 0.95, green: 0.32, blue: 0.36) }
        return .secondary
    }

    static func date(_ value: Date?) -> String {
        guard let value else { return "—" }
        return value.formatted(date: .abbreviated, time: .shortened)
    }
}
