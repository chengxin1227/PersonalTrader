import SwiftUI

struct AlertsView: View {
    @EnvironmentObject private var store: AppStore

    var body: some View {
        NavigationStack {
            List {
                if store.alerts.isEmpty {
                    Text("No alerts yet. When a rule matches, it shows up here and in Slack.")
                        .foregroundStyle(.secondary)
                }
                ForEach(store.alerts) { alert in
                    VStack(alignment: .leading, spacing: 6) {
                        HStack {
                            Text(alert.symbol)
                                .font(.headline.monospaced())
                            Spacer()
                            Text(Formatters.date(alert.firedAt))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        Text(alert.ruleName)
                            .font(.subheadline)
                        Text(alert.message)
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 4)
                }
            }
            .navigationTitle("Alerts")
            .refreshable { await store.refresh() }
        }
    }
}
