import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var settings: AppSettings
    @EnvironmentObject private var store: AppStore
    @State private var slackTestMessage: String?

    var body: some View {
        NavigationStack {
            Form {
                Section("Monitor") {
                    TextField("URL", text: $settings.monitorURL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                    Text("On the simulator, 127.0.0.1 works. On a phone, use your Mac's LAN IP, for example http://192.168.1.10:8080")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                Section("Connection") {
                    statusRow("Monitor running", store.status?.running == true)
                    statusRow("Alpaca keys", store.status?.alpacaConfigured == true)
                    statusRow("Slack webhook", store.status?.slackConfigured == true)
                    LabeledContent("Enabled rules", value: "\(store.status?.enabledRules ?? 0)")
                    if let error = store.status?.lastError {
                        Text(error)
                            .foregroundStyle(.red)
                            .font(.footnote)
                    }
                }

                Section("Slack") {
                    Button("Send test message") {
                        Task {
                            await store.testSlack()
                            slackTestMessage = store.errorMessage == nil
                                ? "Test message sent."
                                : store.errorMessage
                        }
                    }
                    if let slackTestMessage {
                        Text(slackTestMessage)
                            .font(.footnote)
                            .foregroundStyle(store.errorMessage == nil ? Color.secondary : Color.red)
                    }
                }

                Section("Next step") {
                    Text("IBKR order routing comes after this monitor is stable. This app is the control surface; the Python service is what watches the market while the phone is locked.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Settings")
        }
    }

    private func statusRow(_ title: String, _ ok: Bool) -> some View {
        HStack {
            Text(title)
            Spacer()
            Image(systemName: ok ? "checkmark.circle.fill" : "xmark.circle")
                .foregroundStyle(ok ? .green : .orange)
        }
    }
}
