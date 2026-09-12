import Foundation

public protocol RuntimeDiscovering: AnyObject {
    func matchingRuntimePIDs(checkoutRoot: String) throws -> [Int32]
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

    public func matchingRuntimePIDs(checkoutRoot: String) throws -> [Int32] {
        let output = try processListProvider()
        return output.split(whereSeparator: \.isNewline).compactMap { rawLine in
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            guard let separator = line.firstIndex(where: { $0.isWhitespace }) else { return nil }
            guard let pid = Int32(line[..<separator]) else { return nil }
            let command = line[separator...].trimmingCharacters(in: .whitespaces)
            guard Self.matchesRuntimeCommand(command, checkoutRoot: checkoutRoot),
                  let identity = inspector.snapshot(pid: pid),
                  URL(fileURLWithPath: identity.executablePath).lastPathComponent == "tunnel-client" else {
                return nil
            }
            return pid
        }.sorted()
    }

    private static func matchesRuntimeCommand(_ command: String, checkoutRoot: String) -> Bool {
        guard let separator = command.firstIndex(where: { $0.isWhitespace }) else { return false }
        let arguments = command[separator...].trimmingCharacters(in: .whitespaces)
        return arguments == "run --control-plane.poll-channel main --mcp.command command=\(checkoutRoot)/.venv/bin/python -m agent_runtime.server,channel=main --health.listen-addr 127.0.0.1:8080"
    }
}

public struct RuntimeConfiguration: Sendable {
    public let checkoutRoot: String
    public let desiredStateURL: URL
    public let transitionTimeout: TimeInterval

    public init(
        checkoutRoot: String,
        desiredStateURL: URL? = nil,
        transitionTimeout: TimeInterval = 10
    ) {
        self.checkoutRoot = checkoutRoot
        self.desiredStateURL = desiredStateURL ?? FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Agent Runtime", isDirectory: true)
            .appendingPathComponent("protected-runtime-running", isDirectory: false)
        self.transitionTimeout = transitionTimeout
    }
}

public final class NativeRuntimeBackend: RuntimeBackend, @unchecked Sendable {
    private let configuration: RuntimeConfiguration
    private let inspector: ProcessInspecting
    private let discovery: RuntimeDiscovering
    private let legacyStore: OwnershipStoring?
    private let legacySupervisor: OwnedProcessSupervisor?

    public init(
        configuration: RuntimeConfiguration,
        inspector: ProcessInspecting,
        discovery: RuntimeDiscovering
    ) {
        self.configuration = configuration
        self.inspector = inspector
        self.discovery = discovery
        self.legacyStore = nil
        self.legacySupervisor = nil
    }

    public init(
        configuration: RuntimeConfiguration,
        inspector: ProcessInspecting,
        store: OwnershipStoring,
        supervisor: OwnedProcessSupervisor,
        discovery: RuntimeDiscovering
    ) {
        self.configuration = configuration
        self.inspector = inspector
        self.discovery = discovery
        self.legacyStore = store
        self.legacySupervisor = supervisor
    }

    public func observeStatus() throws -> RuntimeStatus {
        if let store = legacyStore {
            if let record = try store.load(),
               record.checkoutRoot == configuration.checkoutRoot,
               let current = inspector.snapshot(pid: record.rootProcess.pid),
               record.matches(current) {
                return .owned(current)
            }
            let external = try discovery.matchingRuntimePIDs(checkoutRoot: configuration.checkoutRoot)
            return external.isEmpty ? .stopped : .external(external)
        }

        let pids = try discovery.matchingRuntimePIDs(checkoutRoot: configuration.checkoutRoot)
        let desiredRunning = FileManager.default.fileExists(atPath: configuration.desiredStateURL.path)
        if !desiredRunning {
            return pids.isEmpty ? .stopped : .external(pids)
        }
        guard pids.count == 1 else {
            if pids.isEmpty {
                return .ambiguous("Desired state is RUNNING, but no canonical Runtime instance is serving.")
            }
            return .ambiguous("Multiple canonical Runtime instances were discovered; refusing lifecycle mutation.")
        }
        guard let identity = inspector.snapshot(pid: pids[0]) else {
            return .ambiguous("Canonical Runtime identity changed during inspection.")
        }
        return .owned(identity)
    }

    public func startOwned() throws {
        if let legacySupervisor {
            let current = try observeStatus()
            guard case .stopped = current else {
                throw RuntimeLifecycleError.actionUnavailable("Runtime is already active or ownership is not available.")
            }
            let script = lifecycleScript()
            let spec = LaunchSpec(
                executablePath: "/bin/bash",
                arguments: [script.path],
                environment: Self.safeChildEnvironment(),
                expectedExecutableName: "tunnel-client",
                transitionTimeout: 10
            )
            _ = try legacySupervisor.start(
                spec: spec,
                profile: "protected-runtime",
                checkoutRoot: configuration.checkoutRoot
            )
            return
        }
        try runLifecycle("start")
        try waitFor(expectedRunning: true)
    }

    public func stopOwned() throws {
        if let legacySupervisor {
            try legacySupervisor.stopOwned()
            return
        }
        try runLifecycle("stop")
        try waitFor(expectedRunning: false)
    }

    public func restartOwned() throws {
        if legacySupervisor != nil {
            try stopOwned()
            try startOwned()
            return
        }
        try runLifecycle("restart")
        try waitFor(expectedRunning: true)
    }

    private func lifecycleScript() -> URL {
        URL(fileURLWithPath: configuration.checkoutRoot)
            .appendingPathComponent("start.sh", isDirectory: false)
    }

    private func runLifecycle(_ action: String) throws {
        let script = lifecycleScript()
        guard FileManager.default.isExecutableFile(atPath: script.path) else {
            throw RuntimeLifecycleError.processLaunchFailed("start.sh is unavailable; run install.sh first")
        }
        let process = Process()
        let stderr = Pipe()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = [script.path, action]
        process.environment = Self.safeChildEnvironment()
        process.standardOutput = FileHandle.nullDevice
        process.standardError = stderr
        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            throw RuntimeLifecycleError.operationFailed("Could not run Runtime lifecycle action: \(error.localizedDescription)")
        }
        guard process.terminationStatus == 0 else {
            let data = stderr.fileHandleForReading.readDataToEndOfFile()
            let detail = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            throw RuntimeLifecycleError.operationFailed(detail.isEmpty ? "Runtime lifecycle action failed." : detail)
        }
    }

    private func waitFor(expectedRunning: Bool) throws {
        let timeout = max(0, configuration.transitionTimeout)
        let deadline = Date().addingTimeInterval(timeout)
        repeat {
            let pids = try discovery.matchingRuntimePIDs(checkoutRoot: configuration.checkoutRoot)
            if expectedRunning {
                if pids.count == 1 { return }
            } else if pids.isEmpty {
                return
            }
            if timeout == 0 { break }
            Thread.sleep(forTimeInterval: 0.05)
        } while Date() < deadline
        throw RuntimeLifecycleError.operationFailed(
            expectedRunning
                ? "Runtime did not converge to exactly one canonical instance."
                : "Runtime did not stop within the bounded transition window."
        )
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
