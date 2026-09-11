import Darwin
import Foundation

public protocol ProcessInspecting: AnyObject {
    func snapshot(pid: Int32) -> ProcessIdentity?
    func groupMembers(processGroupID: Int32) -> [ProcessIdentity]
}

public protocol ProcessSignaling: AnyObject {
    func signalProcessGroup(_ processGroupID: Int32, signal: Int32) -> Int32
    func signalProcess(_ pid: Int32, signal: Int32) -> Int32
}

public final class DarwinProcessSystem: ProcessInspecting, ProcessSignaling, @unchecked Sendable {
    public init() {}

    public func snapshot(pid: Int32) -> ProcessIdentity? {
        guard pid > 0 else { return nil }
        var info = proc_bsdinfo()
        let expected = Int32(MemoryLayout<proc_bsdinfo>.size)
        let read = proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, expected)
        guard read == expected else { return nil }

        var pathBuffer = [CChar](repeating: 0, count: 4096)
        let pathLength = pathBuffer.withUnsafeMutableBytes { buffer -> Int32 in
            proc_pidpath(pid, buffer.baseAddress, UInt32(buffer.count))
        }
        guard pathLength > 0 else { return nil }

        return ProcessIdentity(
            pid: Int32(info.pbi_pid),
            processGroupID: Int32(info.pbi_pgid),
            startSeconds: Int64(info.pbi_start_tvsec),
            startMicroseconds: Int64(info.pbi_start_tvusec),
            executablePath: String(decoding: pathBuffer.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }, as: UTF8.self)
        )
    }

    public func groupMembers(processGroupID: Int32) -> [ProcessIdentity] {
        guard processGroupID > 0 else { return [] }
        let estimated = max(Int(proc_listallpids(nil, 0)), 32)
        var pids = [pid_t](repeating: 0, count: estimated + 32)
        let count = pids.withUnsafeMutableBytes { buffer -> Int32 in
            proc_listallpids(buffer.baseAddress, Int32(buffer.count))
        }
        guard count > 0 else { return [] }
        return pids.prefix(Int(count)).compactMap { pid in
            guard pid > 0, let identity = snapshot(pid: pid), identity.processGroupID == processGroupID else {
                return nil
            }
            return identity
        }
    }

    public func signalProcessGroup(_ processGroupID: Int32, signal: Int32) -> Int32 {
        guard processGroupID > 0 else { return EINVAL }
        return kill(-processGroupID, signal) == 0 ? 0 : errno
    }

    public func signalProcess(_ pid: Int32, signal: Int32) -> Int32 {
        guard pid > 0 else { return EINVAL }
        return kill(pid, signal) == 0 ? 0 : errno
    }
}

public protocol OwnershipStoring: AnyObject {
    func load() throws -> OwnershipRecord?
    func save(_ record: OwnershipRecord) throws
    func clear() throws
}

public final class FileOwnershipStore: OwnershipStoring, @unchecked Sendable {
    public let url: URL
    private let fileManager: FileManager

    public init(url: URL = FileOwnershipStore.defaultURL(), fileManager: FileManager = .default) {
        self.url = url
        self.fileManager = fileManager
    }

    public static func defaultURL() -> URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library", isDirectory: true)
            .appendingPathComponent("Application Support", isDirectory: true)
            .appendingPathComponent("Agent Runtime", isDirectory: true)
            .appendingPathComponent("ownership.json", isDirectory: false)
    }

    public func load() throws -> OwnershipRecord? {
        guard fileManager.fileExists(atPath: url.path) else { return nil }
        try requireRegularNonSymlink(url)
        do {
            let data = try Data(contentsOf: url)
            return try JSONDecoder().decode(OwnershipRecord.self, from: data)
        } catch let error as RuntimeLifecycleError {
            throw error
        } catch {
            throw RuntimeLifecycleError.metadata(error.localizedDescription)
        }
    }

    public func save(_ record: OwnershipRecord) throws {
        let directory = url.deletingLastPathComponent()
        do {
            try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
            _ = chmod(directory.path, mode_t(0o700))
            if fileManager.fileExists(atPath: url.path) {
                try requireRegularNonSymlink(url)
            }
            let data = try JSONEncoder().encode(record)
            try data.write(to: url, options: .atomic)
            _ = chmod(url.path, mode_t(0o600))
        } catch let error as RuntimeLifecycleError {
            throw error
        } catch {
            throw RuntimeLifecycleError.metadata(error.localizedDescription)
        }
    }

    public func clear() throws {
        guard fileManager.fileExists(atPath: url.path) else { return }
        try requireRegularNonSymlink(url)
        do {
            try fileManager.removeItem(at: url)
        } catch {
            throw RuntimeLifecycleError.metadata(error.localizedDescription)
        }
    }

    private func requireRegularNonSymlink(_ candidate: URL) throws {
        do {
            let values = try candidate.resourceValues(forKeys: [.isSymbolicLinkKey, .isRegularFileKey])
            guard values.isSymbolicLink != true, values.isRegularFile == true else {
                throw RuntimeLifecycleError.metadata("ownership path must be a regular non-symlink file")
            }
        } catch let error as RuntimeLifecycleError {
            throw error
        } catch {
            throw RuntimeLifecycleError.metadata(error.localizedDescription)
        }
    }
}

public struct LaunchSpec: Sendable {
    public let executablePath: String
    public let arguments: [String]
    public let environment: [String: String]
    public let expectedExecutableName: String?
    public let transitionTimeout: TimeInterval

    public init(
        executablePath: String,
        arguments: [String],
        environment: [String: String],
        expectedExecutableName: String? = nil,
        transitionTimeout: TimeInterval = 8
    ) {
        self.executablePath = executablePath
        self.arguments = arguments
        self.environment = environment
        self.expectedExecutableName = expectedExecutableName
        self.transitionTimeout = transitionTimeout
    }
}

public protocol ProcessLaunching: AnyObject {
    func launchGroup(_ spec: LaunchSpec) throws -> ProcessIdentity
}

public final class POSIXProcessLauncher: ProcessLaunching, @unchecked Sendable {
    private let inspector: ProcessInspecting
    private let signaler: ProcessSignaling

    public init(inspector: ProcessInspecting, signaler: ProcessSignaling) {
        self.inspector = inspector
        self.signaler = signaler
    }

    public func launchGroup(_ spec: LaunchSpec) throws -> ProcessIdentity {
        var attributes: posix_spawnattr_t? = nil
        guard posix_spawnattr_init(&attributes) == 0 else {
            throw RuntimeLifecycleError.processLaunchFailed("posix_spawnattr_init failed")
        }
        defer { posix_spawnattr_destroy(&attributes) }
        guard posix_spawnattr_setflags(&attributes, Int16(POSIX_SPAWN_SETPGROUP)) == 0,
              posix_spawnattr_setpgroup(&attributes, 0) == 0 else {
            throw RuntimeLifecycleError.processLaunchFailed("could not create an isolated process group")
        }

        var actions: posix_spawn_file_actions_t? = nil
        guard posix_spawn_file_actions_init(&actions) == 0 else {
            throw RuntimeLifecycleError.processLaunchFailed("posix_spawn_file_actions_init failed")
        }
        defer { posix_spawn_file_actions_destroy(&actions) }

        let redirectStatus = "/dev/null".withCString { path -> Int32 in
            let input = posix_spawn_file_actions_addopen(&actions, STDIN_FILENO, path, O_RDONLY, 0)
            guard input == 0 else { return input }
            let output = posix_spawn_file_actions_addopen(&actions, STDOUT_FILENO, path, O_WRONLY, 0)
            guard output == 0 else { return output }
            return posix_spawn_file_actions_addopen(&actions, STDERR_FILENO, path, O_WRONLY, 0)
        }
        guard redirectStatus == 0 else {
            throw RuntimeLifecycleError.processLaunchFailed("could not redirect child stdio")
        }

        var pid: pid_t = 0
        let argv = [spec.executablePath] + spec.arguments
        let env = spec.environment.keys.sorted().map { "\($0)=\(spec.environment[$0]!)" }
        let spawnStatus = withCStringArray(argv) { argvPointer in
            withCStringArray(env) { envPointer in
                spec.executablePath.withCString { executable in
                    posix_spawn(&pid, executable, &actions, &attributes, argvPointer, envPointer)
                }
            }
        }
        guard spawnStatus == 0 else {
            throw RuntimeLifecycleError.processLaunchFailed(String(cString: strerror(spawnStatus)))
        }

        guard let initial = waitForSnapshot(pid: pid, timeout: 0.75), initial.processGroupID == pid else {
            throw RuntimeLifecycleError.processLaunchFailed("spawned process identity could not be verified")
        }

        guard let expectedName = spec.expectedExecutableName else {
            return initial
        }

        let deadline = Date().addingTimeInterval(spec.transitionTimeout)
        while Date() < deadline {
            guard let current = inspector.snapshot(pid: pid) else {
                throw RuntimeLifecycleError.processLaunchFailed("launcher exited before Runtime became active")
            }
            guard initial.isSameProcessInstance(as: current) else {
                throw RuntimeLifecycleError.processLaunchFailed("launcher process identity changed unexpectedly")
            }
            if URL(fileURLWithPath: current.executablePath).lastPathComponent == expectedName {
                return current
            }
            Thread.sleep(forTimeInterval: 0.1)
        }

        cleanupKnownProcess(initial)
        throw RuntimeLifecycleError.processLaunchFailed("Runtime did not reach the expected executable state")
    }

    private func waitForSnapshot(pid: Int32, timeout: TimeInterval) -> ProcessIdentity? {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if let identity = inspector.snapshot(pid: pid) { return identity }
            Thread.sleep(forTimeInterval: 0.02)
        }
        return nil
    }

    private func cleanupKnownProcess(_ identity: ProcessIdentity) {
        guard let current = inspector.snapshot(pid: identity.pid), identity.isSameProcessInstance(as: current) else { return }
        _ = signaler.signalProcessGroup(identity.processGroupID, signal: SIGTERM)
        Thread.sleep(forTimeInterval: 0.2)
        if let current = inspector.snapshot(pid: identity.pid), identity.isSameProcessInstance(as: current) {
            _ = signaler.signalProcess(identity.pid, signal: SIGKILL)
        }
    }

    private func withCStringArray(
        _ values: [String],
        _ body: (UnsafePointer<UnsafeMutablePointer<CChar>?>) -> Int32
    ) -> Int32 {
        var pointers: [UnsafeMutablePointer<CChar>?] = values.map { strdup($0) }
        guard !pointers.contains(where: { $0 == nil }) else {
            for pointer in pointers { if let pointer { free(pointer) } }
            return ENOMEM
        }
        defer { for pointer in pointers { if let pointer { free(pointer) } } }
        pointers.append(nil)
        return pointers.withUnsafeBufferPointer { buffer in
            body(buffer.baseAddress!)
        }
    }
}

public final class OwnedProcessSupervisor: @unchecked Sendable {
    private let inspector: ProcessInspecting
    private let signaler: ProcessSignaling
    private let store: OwnershipStoring
    private let launcher: ProcessLaunching

    public init(
        inspector: ProcessInspecting,
        signaler: ProcessSignaling,
        store: OwnershipStoring,
        launcher: ProcessLaunching
    ) {
        self.inspector = inspector
        self.signaler = signaler
        self.store = store
        self.launcher = launcher
    }

    public func start(
        spec: LaunchSpec,
        profile: String,
        checkoutRoot: String
    ) throws -> OwnershipRecord {
        let identity = try launcher.launchGroup(spec)
        let record = OwnershipRecord(
            rootProcess: identity,
            profile: profile,
            checkoutRoot: checkoutRoot
        )
        do {
            try store.save(record)
            return record
        } catch {
            try? terminateKnown(record: record, clearRecord: false)
            throw error
        }
    }

    public func stopOwned() throws {
        guard let record = try store.load() else {
            throw RuntimeLifecycleError.ownershipMismatch
        }
        try terminateKnown(record: record, clearRecord: true)
    }

    private func terminateKnown(record: OwnershipRecord, clearRecord: Bool) throws {
        guard let root = inspector.snapshot(pid: record.rootProcess.pid), record.matches(root) else {
            throw RuntimeLifecycleError.ownershipMismatch
        }

        let before = inspector.groupMembers(processGroupID: record.rootProcess.processGroupID)
        guard before.contains(where: { record.rootProcess.isSameProcessInstance(as: $0) }) else {
            throw RuntimeLifecycleError.ownershipMismatch
        }
        let known = Dictionary(uniqueKeysWithValues: before.map { ($0.pid, $0) })

        let termStatus = signaler.signalProcessGroup(record.rootProcess.processGroupID, signal: SIGTERM)
        guard termStatus == 0 || termStatus == ESRCH else {
            throw RuntimeLifecycleError.operationFailed("SIGTERM failed: \(String(cString: strerror(termStatus)))")
        }

        if waitForGroupEmpty(record.rootProcess.processGroupID, timeout: 1.5) {
            if clearRecord { try store.clear() }
            return
        }

        let survivors = inspector.groupMembers(processGroupID: record.rootProcess.processGroupID)
        for survivor in survivors {
            guard let original = known[survivor.pid], original.isSameProcessInstance(as: survivor) else {
                throw RuntimeLifecycleError.cleanupAmbiguous
            }
        }
        for survivor in survivors {
            let killStatus = signaler.signalProcess(survivor.pid, signal: SIGKILL)
            guard killStatus == 0 || killStatus == ESRCH else {
                throw RuntimeLifecycleError.operationFailed("SIGKILL failed: \(String(cString: strerror(killStatus)))")
            }
        }

        guard waitForGroupEmpty(record.rootProcess.processGroupID, timeout: 1.0) else {
            throw RuntimeLifecycleError.cleanupIncomplete
        }
        if clearRecord { try store.clear() }
    }

    private func waitForGroupEmpty(_ processGroupID: Int32, timeout: TimeInterval) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if inspector.groupMembers(processGroupID: processGroupID).isEmpty { return true }
            Thread.sleep(forTimeInterval: 0.05)
        }
        return inspector.groupMembers(processGroupID: processGroupID).isEmpty
    }
}
