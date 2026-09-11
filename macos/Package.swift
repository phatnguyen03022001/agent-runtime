// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "AgentRuntimeMenuBar",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "AgentRuntimeCore", targets: ["AgentRuntimeCore"]),
        .executable(name: "AgentRuntimeMenuBar", targets: ["AgentRuntimeMenuBar"]),
    ],
    targets: [
        .target(name: "AgentRuntimeCore"),
        .executableTarget(
            name: "AgentRuntimeMenuBar",
            dependencies: ["AgentRuntimeCore"]
        ),
        .testTarget(
            name: "AgentRuntimeCoreTests",
            dependencies: ["AgentRuntimeCore"]
        ),
    ]
)
