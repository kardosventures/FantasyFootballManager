import AppKit
import ApplicationServices
import Foundation

struct SleeperAppState: Sendable {
    let version: String?
    let running: Bool
    let sessionAvailable: Bool
    let accessibilityGranted: Bool
    let screenRecordingGranted: Bool
    let appPath: String?
}

enum AppInspector {
    static let bundleIdentifier = "com.blitzstudios.sleeperbot"
    static let candidatePaths = [
        "/Applications/Sleeper.app/Wrapper/Sleeper.app",
        "/Applications/Sleeper.app",
    ]

    static func inspect() -> SleeperAppState {
        let url = candidatePaths.map(URL.init(fileURLWithPath:)).first { FileManager.default.fileExists(atPath: $0.path) }
        let bundle = url.flatMap(Bundle.init(url:))
        let version = bundle?.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
        let running = !NSRunningApplication.runningApplications(withBundleIdentifier: bundleIdentifier).isEmpty
        let session = CGSessionCopyCurrentDictionary() as? [String: Any]
        let onConsole = session?[kCGSessionOnConsoleKey as String] as? Bool ?? false
        return SleeperAppState(
            version: version,
            running: running,
            sessionAvailable: onConsole,
            accessibilityGranted: AXIsProcessTrusted(),
            screenRecordingGranted: CGPreflightScreenCaptureAccess(),
            appPath: url?.path
        )
    }
}
