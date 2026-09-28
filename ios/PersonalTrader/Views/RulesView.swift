import SwiftUI

struct RulesView: View {
    @EnvironmentObject private var store: AppStore
    @State private var editorRule = Rule.blank()
    @State private var isEditing = false

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Text("When a rule matches, the monitor sends Slack and records an alert. Cooldown stops repeat pings.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
                Section("Rules") {
                    if store.rulesConfig?.rules.isEmpty ?? true {
                        Text("No rules yet.")
                            .foregroundStyle(.secondary)
                    }
                    ForEach(store.rulesConfig?.rules ?? []) { rule in
                        Button {
                            editorRule = rule
                            isEditing = true
                        } label: {
                            RuleRow(rule: rule)
                        }
                        .foregroundStyle(.primary)
                    }
                    .onDelete { offsets in
                        Task { await store.deleteRules(at: offsets) }
                    }
                }
            }
            .navigationTitle("Rules")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        editorRule = Rule.blank()
                        isEditing = true
                    } label: {
                        Image(systemName: "plus")
                    }
                }
            }
            .sheet(isPresented: $isEditing) {
                RuleEditorView(rule: $editorRule) { saved in
                    try await store.save(rule: saved)
                }
            }
        }
    }
}

private struct RuleRow: View {
    let rule: Rule

    var body: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 4) {
                Text(rule.name)
                    .font(.headline)
                Text("\(rule.symbol) · \(rule.condition.type.title) \(rule.condition.value.formatted())")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Text(rule.enabled ? "On" : "Off")
                .font(.caption.weight(.semibold))
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(rule.enabled ? Color.green.opacity(0.2) : Color.secondary.opacity(0.2))
                .clipShape(Capsule())
        }
        .padding(.vertical, 4)
    }
}
