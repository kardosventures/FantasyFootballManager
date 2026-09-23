import Foundation

struct AgentAPI: Sendable {
    let configuration: AgentConfiguration

    private func request(path: String, method: String = "POST", body: Data? = nil) -> URLRequest {
        var request = URLRequest(url: configuration.apiURL.appending(path: path))
        request.httpMethod = method
        request.httpBody = body
        request.setValue("Bearer \(configuration.sharedSecret)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 15
        return request
    }

    func heartbeat(_ heartbeat: Heartbeat) async throws {
        let (_, response) = try await URLSession.shared.data(for: request(path: "internal/shim/heartbeat", body: try JSONEncoder.managerEncoder.encode(heartbeat)))
        try validate(response)
    }

    func lease() async throws -> ExecutionCommand? {
        let body = try JSONSerialization.data(withJSONObject: ["agent_id": configuration.agentId])
        let (data, response) = try await URLSession.shared.data(for: request(path: "internal/shim/lease", body: body))
        guard let http = response as? HTTPURLResponse else { throw AgentError.request("Missing HTTP response") }
        if http.statusCode == 204 { return nil }
        try validate(response)
        return try JSONDecoder.managerDecoder.decode(ExecutionCommand.self, from: data)
    }

    func report(commandId: String, result: CommandResult) async throws {
        let body = try JSONEncoder.managerEncoder.encode(result)
        let (_, response) = try await URLSession.shared.data(for: request(path: "internal/shim/commands/\(commandId)/result", body: body))
        try validate(response)
    }

    private func validate(_ response: URLResponse) throws {
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw AgentError.request("Manager API rejected the request")
        }
    }
}
