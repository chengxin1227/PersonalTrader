import SwiftUI

@main
struct PersonalTraderApp: App {
    @StateObject private var settings = AppSettings()
    @StateObject private var store: AppStore

    init() {
        let settings = AppSettings()
        _settings = StateObject(wrappedValue: settings)
        _store = StateObject(wrappedValue: AppStore(settings: settings))
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(settings)
                .environmentObject(store)
        }
    }
}
