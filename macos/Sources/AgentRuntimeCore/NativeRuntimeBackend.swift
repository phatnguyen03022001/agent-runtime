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
            guard Self.matchesRuntimeCommand(command, runtimeRoot: checkoutRoot),
                  let identity = inspector.snapshot(pid: pid),
                  URL(fileURLWithPath: identity.executablePath).lastPathComponent == "tunnel-client" else {
                return nil
            }
            return pid
        }.sorted()
    }

    private static func matchesRuntimeCommand(_ command: String, runtimeRoot: String) -> Bool {
        guard let separator = command.firstIndex(where: { $0.isWhitespace }) else { return false }
        let arguments = command[separator...].trimmingCharacters(in: .whitespaces)
        let python = "\(runtimeRoot)/.venv/bin/python"
        let expected = "run --control-plane.poll-channel main --mcp.command command=\(python) -m agent_runtime.server,channel=main --health.listen-addr 127.0.0.1:8080"
        // ps may render an argv element containing a space with shell escaping.
        // Normalize only that presentation detail before exact comparison.
        let normalized = arguments.replacingOccurrences(of: "\\ ", with: " ")
        return normalized == expected
    }
}

public enum RuntimeSessionCapacity {
    public static let fallback = 64

    public static func effective(from envFile: URL?) -> Int {
        guard let envFile,
              let text = try? String(contentsOf: envFile, encoding: .utf8) else {
            return fallback
        }
        for line in text.split(whereSeparator: \.isNewline) {
            let parts = line.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)
            guard parts.count == 2, parts[0] == "AGENT_RUNTIME_MAX_ACTIVE_SESSIONS" else { continue }
            if let value = Int(parts[1].trimmingCharacters(in: .whitespaces)), value > 0 {
                return value
            }
            return fallback
        }
        return fallback
    }
}

public struct RuntimeConfiguration: Sendable {
    public let runtimeRoot: String
    public let envFileURL: URL?
    public let desiredStateURL: URL
    public let transitionTimeout: TimeInterval
    public let requiresReadiness: Bool
    public let sessionLimit: Int

    // Kept as a source-compatible label for fixture callers. In the installed
    // product this value is the package-owned Resources/runtime directory.
    public init(
        checkoutRoot: String,
        desiredStateURL: URL? = nil,
        transitionTimeout: TimeInterval = 10,
        envFileURL: URL? = nil,
        requiresReadiness: Bool = false,
        sessionLimit: Int? = nil
    ) {
        self.runtimeRoot = URL(fileURLWithPath: checkoutRoot).standardizedFileURL.path
        self.envFileURL = envFileURL
        self.desiredStateURL = desiredStateURL ?? FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Agent Runtime", isDirectory: true)
            .appendingPathComponent("protected-runtime-running", isDirectory: false)
        self.transitionTimeout = transitionTimeout
        self.requiresReadiness = requiresReadiness
        self.sessionLimit = sessionLimit ?? RuntimeSessionCapacity.effective(from: envFileURL)
    }

    public init(
        runtimeRoot: String,
        envFileURL: URL,
        desiredStateURL: URL? = nil,
        transitionTimeout: TimeInterval = 10,
        requiresReadiness: Bool = true,
        sessionLimit: Int? = nil
    ) {
        self.init(
            checkoutRoot: runtimeRoot,
            desiredStateURL: desiredStateURL,
            transitionTimeout: transitionTimeout,
            envFileURL: envFileURL,
            requiresReadiness: requiresReadiness,
            sessionLimit: sessionLimit
        )
    }

    public var checkoutRoot: String { runtimeRoot }
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
            let external = try discovery.matchingRuntimePIDs(checkoutRoot: configuration.runtimeRoot)
            return external.isEmpty ? .stopped : .external(external)
        }

        let pids = try discovery.matchingRuntimePIDs(checkoutRoot: configuration.runtimeRoot)
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
        if configuration.requiresReadiness && !readinessIsGreen() {
            return .ambiguous("Canonical Runtime is running but health/readiness is not green yet.")
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
                checkoutRoot: configuration.runtimeRoot
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
        URL(fileURLWithPath: configuration.runtimeRoot)
            .appendingPathComponent("start.sh", isDirectory: false)
    }

    private func runLifecycle(_ action: String) throws {
        let script = lifecycleScript()
        guard FileManager.default.isExecutableFile(atPath: script.path) else {
            throw RuntimeLifecycleError.processLaunchFailed("installed Runtime lifecycle helper is unavailable; run install.sh first")
        }
        let process = Process()
        let stderr = Pipe()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = [script.path, action]
        var environment = Self.safeChildEnvironment()
        if let envFileURL = configuration.envFileURL {
            environment["RUNTIME_ENV_FILE"] = envFileURL.path
        }
        environment["RUNTIME_ROOT"] = configuration.runtimeRoot
        process.environment = environment
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
            let pids = try discovery.matchingRuntimePIDs(checkoutRoot: configuration.runtimeRoot)
            if expectedRunning {
                if pids.count == 1 && (!configuration.requiresReadiness || readinessIsGreen()) { return }
            } else if pids.isEmpty {
                return
            }
            if timeout == 0 { break }
            Thread.sleep(forTimeInterval: 0.05)
        } while Date() < deadline
        throw RuntimeLifecycleError.operationFailed(
            expectedRunning
                ? "Runtime did not converge to exactly one healthy canonical instance."
                : "Runtime did not stop within the bounded transition window."
        )
    }

    private func readinessIsGreen() -> Bool {
        for endpoint in ["healthz", "readyz"] {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/curl")
            process.arguments = ["-fsS", "--max-time", "1", "http://127.0.0.1:8080/\(endpoint)"]
            process.standardOutput = FileHandle.nullDevice
            process.standardError = FileHandle.nullDevice
            do {
                try process.run()
                process.waitUntilExit()
            } catch {
                return false
            }
            guard process.terminationStatus == 0 else { return false }
        }
        return true
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
