// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "AgentRuntimeMenuBar",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "AgentRuntimeCore", targets: ["AgentRuntimeCore"]),
        .executable(name: "AgentRuntimeMenuBar", targets: ["AgentRuntimeMenuBar"]),
        .executable(name: "AgentRuntimeRuntimeService", targets: ["AgentRuntimeRuntimeService"]),
    ],
    targets: [
        .target(name: "AgentRuntimeCore"),
        .executableTarget(
            name: "AgentRuntimeMenuBar",
            dependencies: ["AgentRuntimeCore"]
        ),
        .executableTarget(name: "AgentRuntimeRuntimeService"),
        .testTarget(
            name: "AgentRuntimeCoreTests",
            dependencies: ["AgentRuntimeCore"]
        ),
        .testTarget(
            name: "AgentRuntimeMenuBarTests",
            dependencies: ["AgentRuntimeMenuBar", "AgentRuntimeCore"]
        ),
    ]
)
