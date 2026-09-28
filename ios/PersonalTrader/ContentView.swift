import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var store: AppStore

    var body: some View {
        TabView {
            WatchlistView()
                .tabItem { Label("Watchlist", systemImage: "chart.line.uptrend.xyaxis") }
            RulesView()
                .tabItem { Label("Rules", systemImage: "slider.horizontal.3") }
            AlertsView()
                .tabItem { Label("Alerts", systemImage: "bell") }
            SettingsView()
                .tabItem { Label("Settings", systemImage: "gearshape") }
        }
        .task { await store.refresh() }
        .overlay(alignment: .top) {
            if let message = store.errorMessage {
                Text(message)
                    .font(.footnote)
                    .padding(10)
                    .frame(maxWidth: .infinity)
                    .background(.red.opacity(0.9))
                    .foregroundStyle(.white)
            }
        }
    }
}
