import Foundation

@MainActor
final class AppStore: ObservableObject {
    @Published var quotes: [Quote] = []
    @Published var rulesConfig: RulesConfig?
    @Published var alerts: [AlertItem] = []
    @Published var status: MonitorStatus?
    @Published var isLoading = false
    @Published var errorMessage: String?

    private let settings: AppSettings

    init(settings: AppSettings) {
        self.settings = settings
    }

    var api: MonitorAPI? {
        guard let url = URL(string: settings.monitorURL), url.scheme != nil else {
            return nil
        }
        return MonitorAPI(baseURL: url)
    }

    func refresh() async {
        guard let api else {
            errorMessage = "Set a valid monitor URL in Settings."
            return
        }
        isLoading = true
        defer { isLoading = false }
        do {
            async let statusTask = api.status()
            async let quotesTask = api.quotes()
            async let rulesTask = api.rules()
            async let alertsTask = api.alerts()
            status = try await statusTask
            quotes = try await quotesTask
            rulesConfig = try await rulesTask
            alerts = try await alertsTask
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func pollNow() async {
        guard let api else { return }
        do {
            _ = try await api.poll()
            await refresh()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func save(rule: Rule) async throws {
        guard let api else { throw MonitorAPIError.invalidURL }
        rulesConfig = try await api.upsert(rule: rule)
        await refresh()
    }

    func deleteRules(at offsets: IndexSet) async {
        guard let api, let rules = rulesConfig?.rules else { return }
        for index in offsets {
            do {
                rulesConfig = try await api.deleteRule(id: rules[index].id)
            } catch {
                errorMessage = error.localizedDescription
            }
        }
        await refresh()
    }

    func testSlack() async {
        guard let api else { return }
        do {
            try await api.testSlack()
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}
