import Foundation
import SwiftUI

@MainActor
final class AppSettings: ObservableObject {
    @AppStorage("monitorURL") var monitorURL: String = "http://127.0.0.1:8080"
}
