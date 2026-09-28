import SwiftUI

struct WatchlistView: View {
    @EnvironmentObject private var store: AppStore

    var body: some View {
        NavigationStack {
            List {
                if let status = store.status {
                    Section {
                        HStack {
                            Label(
                                status.market?.isOpen == true ? "Market open" : "Market closed",
                                systemImage: status.market?.isOpen == true ? "clock.fill" : "clock"
                            )
                            Spacer()
                            Text("Last poll \(Formatters.date(status.lastPollAt))")
                                .foregroundStyle(.secondary)
                                .font(.footnote)
                        }
                    }
                }

                Section("Quotes") {
                    if store.quotes.isEmpty {
                        Text("No quotes yet. Start the monitor, then pull to refresh.")
                            .foregroundStyle(.secondary)
                    }
                    ForEach(store.quotes) { quote in
                        QuoteRow(quote: quote)
                    }
                }
            }
            .navigationTitle("Watchlist")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        Task { await store.pollNow() }
                    } label: {
                        Image(systemName: "arrow.triangle.2.circlepath")
                    }
                }
            }
            .refreshable { await store.refresh() }
        }
    }
}

private struct QuoteRow: View {
    let quote: Quote

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 4) {
                Text(quote.symbol)
                    .font(.headline.monospaced())
                if let volume = quote.volume {
                    Text("Vol \(volume.formatted(.number.notation(.compactName)))")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 4) {
                Text(Formatters.price(quote.price))
                    .font(.headline.monospacedDigit())
                Text(Formatters.percent(quote.changePct))
                    .font(.subheadline.monospacedDigit())
                    .foregroundStyle(Formatters.changeColor(quote.changePct))
            }
        }
        .padding(.vertical, 4)
    }
}
