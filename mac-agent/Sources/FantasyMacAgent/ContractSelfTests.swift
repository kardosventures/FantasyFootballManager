import Foundation

enum ContractSelfTests {
    static func run() throws {
        let valid = Data("""
        {"id":"c1","action_type":"DRAFT_PLAYER","league_id":"l1","roster_id":8,"parameters":{"player_id":"p1"},"expected_state_hash":"abc","decision_policy_version":"2026.1","evidence_hashes":["ev1"],"idempotency_key":"key1","not_before":"2026-09-05T01:29:00Z","expires_at":"2026-09-05T01:31:00Z","verification_plan":{"endpoint":"draft/d1/picks"}}
        """.utf8)
        let command = try JSONDecoder.managerDecoder.decode(ExecutionCommand.self, from: valid)
        guard command.actionType == .draftPlayer,
              command.rosterId == 8,
              command.parameters["player_id"] == .string("p1") else {
            throw AgentError.unsupported("Valid semantic contract did not round-trip")
        }
        let forbidden = Data("""
        {"id":"c1","action_type":"TRADE","league_id":"l1","roster_id":8,"parameters":{},"expected_state_hash":"abc","decision_policy_version":"2026.1","evidence_hashes":[],"idempotency_key":"key1","not_before":"2026-09-05T01:29:00Z","expires_at":"2026-09-05T01:31:00Z","verification_plan":{}}
        """.utf8)
        do {
            _ = try JSONDecoder.managerDecoder.decode(ExecutionCommand.self, from: forbidden)
            throw AgentError.unsupported("Forbidden TRADE command decoded")
        } catch is DecodingError {
            // Expected: forbidden actions are outside the semantic command enum.
        }
        print("mac-agent contract self-tests passed")
    }
}
