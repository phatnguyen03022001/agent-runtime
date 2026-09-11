import Foundation

public protocol RuntimeDiscovering: AnyObject {
    func matchingRuntimePIDs(profile: String) throws -> [Int32]
}

public final class PSRuntimeDiscovery: RuntimeDiscovering, @unchecked Sendable {
    private let inspector: ProcessInspecting
    private let processListProvider: () throws -> String

    public init(inspector: ProcessInspecting) {
        self.inspector = inspector
        self.processListProvider = Self.readProcessList
    }

    init(inspector: ProcessInspecting, processListProvider: @escaping () throws -> String) {
        self.inspector = inspector
        self.processListProvider = processListProvider
    }

    private static func readProcessList() throws -> String {
        let process = Process()
        let pipe = Pipe()
        process.executableURL = URL(fileURLWithPath: "/bin/ps")
        process.arguments = ["-ax", "-o", "pid=,command="]
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            throw RuntimeLifecycleError.operationFailed("Could not inspect Runtime processes: \(error.localizedDescription)")
        }
        guard process.terminationStatus == 0 else {
            throw RuntimeLifecycleError.operationFailed("Could not inspect Runtime processes.")
        }

        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        return String(decoding: data, as: UTF8.self)
    }

    public func matchingRuntimePIDs(profile: String) throws -> [Int32] {
        let output = try processListProvider()
        return output.split(whereSeparator: \.isNewline).compactMap { rawLine in
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            guard let separator = line.firstIndex(where: { $0.isWhitespace }) else { return nil }
            guard let pid = Int32(line[..<separator]) else { return nil }
            let command = line[separator...].trimmingCharacters(in: .whitespaces)
            guard Self.matchesRuntimeCommand(command, profile: profile),
                  let identity = inspector.snapshot(pid: pid),
                  URL(fileURLWithPath: identity.executablePath).lastPathComponent == "tunnel-client" else {
                return nil
            }
            return pid
        }.sorted()
    }

    private static func matchesRuntimeCommand(_ command: String, profile: String) -> Bool {
        guard let separator = command.firstIndex(where: { $0.isWhitespace }) else { return false }
        let arguments = command[separator...].trimmingCharacters(in: .whitespaces)
        let canonicalProfileFile = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".config/tunnel-client/\(profile).yaml").path
        return arguments == "run --profile-file \(canonicalProfileFile)"
            || arguments == "run --profile \(profile)"
    }
}

public struct RuntimeConfiguration: Sendable {
    public let checkoutRoot: String
    public let profile: String

    public init(checkoutRoot: String, profile: String = "agent-runtime") {
        self.checkoutRoot = checkoutRoot
        self.profile = profile
    }
}

public final class NativeRuntimeBackend: RuntimeBackend, @unchecked Sendable {
    private let configuration: RuntimeConfiguration
    private let inspector: ProcessInspecting
    private let store: OwnershipStoring
    private let supervisor: OwnedProcessSupervisor
    private let discovery: RuntimeDiscovering

    public init(
        configuration: RuntimeConfiguration,
        inspector: ProcessInspecting,
        store: OwnershipStoring,
        supervisor: OwnedProcessSupervisor,
        discovery: RuntimeDiscovering
    ) {
        self.configuration = configuration
        self.inspector = inspector
        self.store = store
        self.supervisor = supervisor
        self.discovery = discovery
    }

    public func observeStatus() throws -> RuntimeStatus {
        if let record = try store.load(),
           record.profile == configuration.profile,
           record.checkoutRoot == configuration.checkoutRoot,
           let current = inspector.snapshot(pid: record.rootProcess.pid),
           record.matches(current) {
            return .owned(current)
        }

        let external = try discovery.matchingRuntimePIDs(profile: configuration.profile)
        if !external.isEmpty {
            return .external(external)
        }
        return .stopped
    }

    public func startOwned() throws {
        let current = try observeStatus()
        guard case .stopped = current else {
            throw RuntimeLifecycleError.actionUnavailable("Runtime is already active or ownership is not available.")
        }
        let script = URL(fileURLWithPath: configuration.checkoutRoot)
            .appendingPathComponent("start.sh", isDirectory: false)
        guard FileManager.default.isExecutableFile(atPath: script.path) else {
            throw RuntimeLifecycleError.processLaunchFailed("start.sh is unavailable; run install.sh first")
        }

        let spec = LaunchSpec(
            executablePath: "/bin/bash",
            arguments: [script.path],
            environment: Self.safeChildEnvironment(),
            expectedExecutableName: "tunnel-client",
            transitionTimeout: 10
        )
        _ = try supervisor.start(
            spec: spec,
            profile: configuration.profile,
            checkoutRoot: configuration.checkoutRoot
        )
    }

    public func stopOwned() throws {
        try supervisor.stopOwned()
    }

    private static func safeChildEnvironment() -> [String: String] {
        let source = ProcessInfo.processInfo.environment
        var result: [String: String] = [:]
        for key in ["HOME", "USER", "TMPDIR", "LANG"] {
            if let value = source[key], !value.isEmpty { result[key] = value }
        }
        for (key, value) in source where key.hasPrefix("LC_") && !value.isEmpty {
            result[key] = value
        }
        var pathParts = (source["PATH"] ?? "").split(separator: ":").map(String.init)
        for candidate in ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
            where !pathParts.contains(candidate) {
            pathParts.append(candidate)
        }
        result["PATH"] = pathParts.joined(separator: ":")
        return result
    }
}
