import SwiftUI

struct RuleEditorView: View {
    @Binding var rule: Rule
    var onSave: (Rule) async throws -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var errorMessage: String?
    @State private var isSaving = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Rule") {
                    TextField("Name", text: $rule.name)
                    TextField("Symbol", text: $rule.symbol)
                        .textInputAutocapitalization(.characters)
                    Toggle("Enabled", isOn: $rule.enabled)
                }
                Section("Condition") {
                    Picker("Type", selection: $rule.condition.type) {
                        ForEach(ConditionType.allCases) { type in
                            Text(type.title).tag(type)
                        }
                    }
                    HStack {
                        Text("Value")
                        Spacer()
                        TextField("0", value: $rule.condition.value, format: .number)
                            .keyboardType(.decimalPad)
                            .multilineTextAlignment(.trailing)
                    }
                    Stepper("Cooldown \(rule.cooldownMinutes) min", value: $rule.cooldownMinutes, in: 0 ... 1440, step: 15)
                }
                Section("Slack message") {
                    TextField("Template", text: slackMessageBinding, axis: .vertical)
                        .lineLimit(3 ... 6)
                    Text("Tokens: {name} {symbol} {price} {change_pct} {threshold}")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if let errorMessage {
                    Section {
                        Text(errorMessage)
                            .foregroundStyle(.red)
                    }
                }
            }
            .navigationTitle(rule.id.isEmpty ? "New rule" : "Edit rule")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        Task { await save() }
                    }
                    .disabled(isSaving)
                }
            }
        }
    }

    private var slackMessageBinding: Binding<String> {
        Binding(
            get: { rule.slackMessage ?? "" },
            set: { rule.slackMessage = $0.isEmpty ? nil : $0 }
        )
    }

    private func save() async {
        var prepared = rule
        prepared.symbol = prepared.symbol.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        prepared.name = prepared.name.trimmingCharacters(in: .whitespacesAndNewlines)
        if prepared.id.isEmpty {
            prepared.id = "\(prepared.symbol.lowercased())-\(prepared.condition.type.rawValue)-\(Int(prepared.condition.value))"
        }
        guard !prepared.name.isEmpty, !prepared.symbol.isEmpty else {
            errorMessage = "Name and symbol are required."
            return
        }
        isSaving = true
        defer { isSaving = false }
        do {
            try await onSave(prepared)
            dismiss()
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}
