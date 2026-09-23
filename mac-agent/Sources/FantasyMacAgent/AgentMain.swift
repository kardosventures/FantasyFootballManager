import Foundation

@main
struct FantasyMacAgent {
    static func main() async {
        do {
            if CommandLine.arguments.contains("--self-test") {
                try ContractSelfTests.run()
                return
            }
            let configuration = try AgentConfiguration.environment()
            let api = AgentAPI(configuration: configuration)
            let executor = SafeSleeperExecutor(configuration: configuration)
            while !Task.isCancelled {
                let app = AppInspector.inspect()
                let heartbeat = Heartbeat(
                    agentId: configuration.agentId,
                    appVersion: app.version,
                    appRunning: app.running,
                    sessionAvailable: app.sessionAvailable,
                    executionMode: configuration.mode,
                    capabilities: configuration.mode == "desktop" ? [] : ActionType.allCases.map(\.rawValue),
                    details: [
                        "accessibility_granted": .bool(app.accessibilityGranted),
                        "screen_recording_granted": .bool(app.screenRecordingGranted),
                        "qualified_version": .string(configuration.qualifiedSleeperVersion),
                    ]
                )
                do {
                    try await api.heartbeat(heartbeat)
                    if let command = try await api.lease() {
                        try await api.report(
                            commandId: command.id,
                            result: CommandResult(
                                agentId: configuration.agentId,
                                status: "preflight",
                                message: "macOS agent started semantic command preflight",
                                evidence: ["app_version": .string(app.version ?? "unknown")]
                            )
                        )
                        let result = await executor.execute(command, app: app)
                        try await api.report(commandId: command.id, result: result)
                    }
                } catch {
                    FileHandle.standardError.write(Data("mac-agent: \(error.localizedDescription)\n".utf8))
                }
                try? await Task.sleep(for: .seconds(configuration.pollSeconds))
            }
        } catch {
            FileHandle.standardError.write(Data("mac-agent configuration: \(error.localizedDescription)\n".utf8))
            Foundation.exit(78)
        }
    }
}
