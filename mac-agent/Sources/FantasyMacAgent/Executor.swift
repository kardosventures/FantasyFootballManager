import Foundation

protocol CommandExecutor: Sendable {
    func execute(_ command: ExecutionCommand, app: SleeperAppState) async -> CommandResult
}

struct SafeSleeperExecutor: CommandExecutor {
    let configuration: AgentConfiguration

    func execute(_ command: ExecutionCommand, app: SleeperAppState) async -> CommandResult {
        let baseEvidence: [String: JSONValue] = [
            "app_version": .string(app.version ?? "unknown"),
            "app_path": .string(app.appPath ?? "not-found"),
            "idempotency_key": .string(command.idempotencyKey),
            "mode": .string(configuration.mode),
        ]
        guard Date() < command.expiresAt else {
            return result("blocked", "Command expired before preflight", command, baseEvidence)
        }
        guard app.version == configuration.qualifiedSleeperVersion else {
            return result("blocked", "Sleeper version is unqualified; writes remain locked", command, baseEvidence)
        }
        guard app.running, app.sessionAvailable else {
            return result("blocked", "Sleeper or the logged-in console session is unavailable", command, baseEvidence)
        }
        guard app.accessibilityGranted else {
            return result("blocked", "Accessibility permission is missing", command, baseEvidence)
        }
        if configuration.mode == "dry_run" || configuration.mode == "fake" {
            var evidence = baseEvidence
            evidence["before_state_hash"] = .string(command.expectedStateHash)
            evidence["semantic_action"] = .string(command.actionType.rawValue)
            evidence["write_attempted"] = .bool(false)
            return result("verified", "Dry-run semantic command passed preflight; no Sleeper write was attempted", command, evidence)
        }
        // Intentionally fail closed. Action-specific UI drivers are enabled only after recorded-tree tests and mock-draft qualification.
        return result("blocked", "Live UI driver for \(command.actionType.rawValue) is not qualified", command, baseEvidence)
    }

    private func result(_ status: String, _ message: String, _ command: ExecutionCommand, _ evidence: [String: JSONValue]) -> CommandResult {
        CommandResult(agentId: configuration.agentId, status: status, message: message, evidence: evidence)
    }
}
