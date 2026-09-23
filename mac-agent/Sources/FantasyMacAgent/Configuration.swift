import Foundation

struct AgentConfiguration: Sendable {
    let apiURL: URL
    let sharedSecret: String
    let agentId: String
    let mode: String
    let qualifiedSleeperVersion: String
    let pollSeconds: UInt64
    let codexEnabled: Bool
    let codexPath: String

    static func environment(_ environment: [String: String] = ProcessInfo.processInfo.environment) throws -> AgentConfiguration {
        guard let apiURL = URL(string: environment["FANTASY_API_URL"] ?? "http://127.0.0.1:8000") else {
            throw AgentError.configuration("FANTASY_API_URL is invalid")
        }
        let secret = environment["SHIM_SHARED_SECRET"] ?? ""
        guard secret.count >= 20 else { throw AgentError.configuration("SHIM_SHARED_SECRET must be configured") }
        return AgentConfiguration(
            apiURL: apiURL,
            sharedSecret: secret,
            agentId: environment["FANTASY_AGENT_ID"] ?? "mac-agent-primary",
            mode: environment["EXECUTION_MODE"] ?? "dry_run",
            qualifiedSleeperVersion: environment["QUALIFIED_SLEEPER_VERSION"] ?? "149.1",
            pollSeconds: UInt64(environment["AGENT_POLL_SECONDS"] ?? "10") ?? 10,
            codexEnabled: environment["CODEX_EXPLANATIONS_ENABLED"] == "true",
            codexPath: environment["CODEX_BIN"] ?? "/usr/local/bin/codex"
        )
    }
}

enum AgentError: Error, LocalizedError {
    case configuration(String), request(String), preflight(String), unsupported(String)
    var errorDescription: String? {
        switch self {
        case .configuration(let value), .request(let value), .preflight(let value), .unsupported(let value): value
        }
    }
}
