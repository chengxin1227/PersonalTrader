import Foundation

enum ConditionType: String, Codable, CaseIterable, Identifiable {
    case priceAbove = "price_above"
    case priceBelow = "price_below"
    case percentChange = "percent_change"
    case crossesAbove = "crosses_above"
    case crossesBelow = "crosses_below"

    var id: String { rawValue }

    var title: String {
        switch self {
        case .priceAbove: "Price above"
        case .priceBelow: "Price below"
        case .percentChange: "Daily % change"
        case .crossesAbove: "Crosses above"
        case .crossesBelow: "Crosses below"
        }
    }
}

struct Condition: Codable, Hashable {
    var type: ConditionType
    var value: Double
}

struct Rule: Codable, Identifiable, Hashable {
    var id: String
    var name: String
    var symbol: String
    var enabled: Bool
    var cooldownMinutes: Int
    var condition: Condition
    var slackMessage: String?

    enum CodingKeys: String, CodingKey {
        case id, name, symbol, enabled, condition
        case cooldownMinutes = "cooldown_minutes"
        case slackMessage = "slack_message"
    }

    static func blank() -> Rule {
        Rule(
            id: "",
            name: "",
            symbol: "",
            enabled: true,
            cooldownMinutes: 60,
            condition: Condition(type: .priceAbove, value: 0),
            slackMessage: "{name}: {symbol} is ${price:.2f} ({change_pct:+.2f}% today)"
        )
    }
}

struct RulesConfig: Codable {
    var pollIntervalSeconds: Int
    var pollWhenClosed: Bool
    var feed: String
    var watchlist: [String]
    var rules: [Rule]

    enum CodingKeys: String, CodingKey {
        case feed, watchlist, rules
        case pollIntervalSeconds = "poll_interval_seconds"
        case pollWhenClosed = "poll_when_closed"
    }
}

struct Quote: Codable, Identifiable, Hashable {
    var symbol: String
    var price: Double?
    var bid: Double?
    var ask: Double?
    var dailyOpen: Double?
    var dailyHigh: Double?
    var dailyLow: Double?
    var prevClose: Double?
    var changePct: Double?
    var volume: Int?
    var updatedAt: Date?

    var id: String { symbol }

    enum CodingKeys: String, CodingKey {
        case symbol, price, bid, ask, volume
        case dailyOpen = "daily_open"
        case dailyHigh = "daily_high"
        case dailyLow = "daily_low"
        case prevClose = "prev_close"
        case changePct = "change_pct"
        case updatedAt = "updated_at"
    }
}

struct MarketClock: Codable, Hashable {
    var isOpen: Bool
    var nextOpen: Date?
    var nextClose: Date?
    var timestamp: Date?

    enum CodingKeys: String, CodingKey {
        case timestamp
        case isOpen = "is_open"
        case nextOpen = "next_open"
        case nextClose = "next_close"
    }
}

struct MonitorStatus: Codable, Hashable {
    var running: Bool
    var lastPollAt: Date?
    var lastError: String?
    var market: MarketClock?
    var watchlist: [String]
    var enabledRules: Int
    var slackConfigured: Bool
    var alpacaConfigured: Bool

    enum CodingKeys: String, CodingKey {
        case running, market, watchlist
        case lastPollAt = "last_poll_at"
        case lastError = "last_error"
        case enabledRules = "enabled_rules"
        case slackConfigured = "slack_configured"
        case alpacaConfigured = "alpaca_configured"
    }
}

struct AlertItem: Codable, Identifiable, Hashable {
    var id: String
    var ruleId: String
    var ruleName: String
    var symbol: String
    var message: String
    var price: Double?
    var changePct: Double?
    var firedAt: Date

    enum CodingKeys: String, CodingKey {
        case id, symbol, message, price
        case ruleId = "rule_id"
        case ruleName = "rule_name"
        case changePct = "change_pct"
        case firedAt = "fired_at"
    }
}
