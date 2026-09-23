// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "FantasyMacAgent",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "FantasyMacAgent", targets: ["FantasyMacAgent"])],
    targets: [
        .executableTarget(name: "FantasyMacAgent"),
    ]
)
