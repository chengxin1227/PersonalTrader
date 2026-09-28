import Foundation

enum MonitorAPIError: LocalizedError {
    case invalidURL
    case badResponse(Int, String)
    case decoding(Error)

    var errorDescription: String? {
        switch self {
        case .invalidURL:
            return "Monitor URL is invalid."
        case .badResponse(let code, let body):
            return "Monitor returned \(code): \(body)"
        case .decoding(let error):
            return "Could not read monitor response: \(error.localizedDescription)"
        }
    }
}

struct MonitorAPI {
    var baseURL: URL

    private var decoder: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let raw = try container.decode(String.self)
            let iso = ISO8601DateFormatter()
            iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            if let date = iso.date(from: raw) {
                return date
            }
            iso.formatOptions = [.withInternetDateTime]
            if let date = iso.date(from: raw) {
                return date
            }
            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "Invalid date: \(raw)"
            )
        }
        return decoder
    }

    private var encoder: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.sortedKeys]
        return encoder
    }

    func status() async throws -> MonitorStatus {
        try await get("/status")
    }

    func quotes() async throws -> [Quote] {
        try await get("/quotes")
    }

    func rules() async throws -> RulesConfig {
        try await get("/rules")
    }

    func alerts() async throws -> [AlertItem] {
        try await get("/alerts")
    }

    func poll() async throws -> [AlertItem] {
        try await send(path: "/poll", method: "POST")
    }

    func testSlack() async throws {
        let _: [String: String] = try await send(path: "/alerts/test", method: "POST")
    }

    func upsert(rule: Rule) async throws -> RulesConfig {
        try await send(path: "/rules/\(rule.id)", method: "PUT", body: rule)
    }

    func deleteRule(id: String) async throws -> RulesConfig {
        try await send(path: "/rules/\(id)", method: "DELETE")
    }

    func replace(config: RulesConfig) async throws -> RulesConfig {
        try await send(path: "/rules", method: "PUT", body: config)
    }

    private func get<T: Decodable>(_ path: String) async throws -> T {
        try await send(path: path, method: "GET")
    }

    private func send<T: Decodable, Body: Encodable>(
        path: String,
        method: String,
        body: Body
    ) async throws -> T {
        try await send(path: path, method: method, data: try encoder.encode(body))
    }

    private func send<T: Decodable>(
        path: String,
        method: String,
        data: Data? = nil
    ) async throws -> T {
        guard let url = URL(string: path, relativeTo: baseURL) else {
            throw MonitorAPIError.invalidURL
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let data {
            request.httpBody = data
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let (payload, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw MonitorAPIError.badResponse(-1, "No HTTP response")
        }
        guard (200 ... 299).contains(http.statusCode) else {
            let body = String(data: payload, encoding: .utf8) ?? ""
            throw MonitorAPIError.badResponse(http.statusCode, body)
        }
        do {
            return try decoder.decode(T.self, from: payload)
        } catch {
            throw MonitorAPIError.decoding(error)
        }
    }
}
