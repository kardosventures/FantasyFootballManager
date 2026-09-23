import Foundation

enum ActionType: String, Codable, CaseIterable, Sendable {
    case draftPlayer = "DRAFT_PLAYER"
    case setLineup = "SET_LINEUP"
    case moveToIR = "MOVE_TO_IR"
    case removeFromIR = "REMOVE_FROM_IR"
    case setAutoSub = "SET_AUTOSUB"
    case waiverClaim = "WAIVER_CLAIM"
    case addFreeAgent = "ADD_FREE_AGENT"
    case dropPlayer = "DROP_PLAYER"
}

struct ExecutionCommand: Codable, Sendable {
    let id: String
    let actionType: ActionType
    let leagueId: String
    let rosterId: Int
    let parameters: [String: JSONValue]
    let expectedStateHash: String
    let decisionPolicyVersion: String
    let evidenceHashes: [String]
    let idempotencyKey: String
    let notBefore: Date
    let expiresAt: Date
    let verificationPlan: [String: JSONValue]
}

enum JSONValue: Codable, Sendable, Equatable {
    case string(String), number(Double), bool(Bool), object([String: JSONValue]), array([JSONValue]), null

    init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer()
        if value.decodeNil() { self = .null }
        else if let item = try? value.decode(Bool.self) { self = .bool(item) }
        else if let item = try? value.decode(Double.self) { self = .number(item) }
        else if let item = try? value.decode(String.self) { self = .string(item) }
        else if let item = try? value.decode([String: JSONValue].self) { self = .object(item) }
        else { self = .array(try value.decode([JSONValue].self)) }
    }

    func encode(to encoder: Encoder) throws {
        var value = encoder.singleValueContainer()
        switch self {
        case .string(let item): try value.encode(item)
        case .number(let item): try value.encode(item)
        case .bool(let item): try value.encode(item)
        case .object(let item): try value.encode(item)
        case .array(let item): try value.encode(item)
        case .null: try value.encodeNil()
        }
    }
}

struct Heartbeat: Codable, Sendable {
    let agentId: String
    let appVersion: String?
    let appRunning: Bool
    let sessionAvailable: Bool
    let executionMode: String
    let capabilities: [String]
    let details: [String: JSONValue]
}

struct CommandResult: Codable, Sendable {
    let agentId: String
    let status: String
    let message: String
    let evidence: [String: JSONValue]
}

extension JSONDecoder {
    static var managerDecoder: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }
}

extension JSONEncoder {
    static var managerEncoder: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        encoder.dateEncodingStrategy = .iso8601
        return encoder
    }
}
