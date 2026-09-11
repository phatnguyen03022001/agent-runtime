import Foundation

public struct ProcessIdentity: Codable, Equatable, Sendable {
    public let pid: Int32
    public let processGroupID: Int32
    public let startSeconds: Int64
    public let startMicroseconds: Int64
    public let executablePath: String

    public init(
        pid: Int32,
        processGroupID: Int32,
        startSeconds: Int64,
        startMicroseconds: Int64,
        executablePath: String
    ) {
        self.pid = pid
        self.processGroupID = processGroupID
        self.startSeconds = startSeconds
        self.startMicroseconds = startMicroseconds
        self.executablePath = executablePath
    }

    public func isSameProcessInstance(as other: ProcessIdentity) -> Bool {
        pid == other.pid
            && processGroupID == other.processGroupID
            && startSeconds == other.startSeconds
            && startMicroseconds == other.startMicroseconds
    }
}

public struct OwnershipRecord: Codable, Equatable, Sendable {
    public let version: Int
    public let rootProcess: ProcessIdentity
    public let profile: String
    public let checkoutRoot: String

    public init(
        version: Int = 1,
        rootProcess: ProcessIdentity,
        profile: String,
        checkoutRoot: String
    ) {
        self.version = version
        self.rootProcess = rootProcess
        self.profile = profile
        self.checkoutRoot = checkoutRoot
    }

    public func matches(_ current: ProcessIdentity) -> Bool {
        version == 1
            && rootProcess.isSameProcessInstance(as: current)
            && rootProcess.executablePath == current.executablePath
    }
}

public enum RuntimeStatus: Equatable, Sendable {
    case stopped
    case owned(ProcessIdentity)
    case external([Int32])
    case ambiguous(String)
}

public enum RuntimeAction: Sendable {
    case start
    case stop
    case restart
}

public struct RuntimeActionAvailability: Equatable, Sendable {
    public let canStart: Bool
    public let canStop: Bool
    public let canRestart: Bool

    public init(canStart: Bool, canStop: Bool, canRestart: Bool) {
        self.canStart = canStart
        self.canStop = canStop
        self.canRestart = canRestart
    }
}

public enum RuntimePolicy {
    public static func actions(for status: RuntimeStatus) -> RuntimeActionAvailability {
        switch status {
        case .stopped:
            return RuntimeActionAvailability(canStart: true, canStop: false, canRestart: false)
        case .owned:
            return RuntimeActionAvailability(canStart: false, canStop: true, canRestart: true)
        case .external, .ambiguous:
            return RuntimeActionAvailability(canStart: false, canStop: false, canRestart: false)
        }
    }
}

public enum RuntimeLifecycleError: Error, Equatable, LocalizedError, Sendable {
    case actionUnavailable(String)
    case ownershipMismatch
    case processLaunchFailed(String)
    case cleanupAmbiguous
    case cleanupIncomplete
    case metadata(String)
    case operationFailed(String)

    public var errorDescription: String? {
        switch self {
        case .actionUnavailable(let message): return message
        case .ownershipMismatch: return "Runtime ownership could not be positively revalidated."
        case .processLaunchFailed(let message): return "Could not start Runtime: \(message)"
        case .cleanupAmbiguous: return "Runtime cleanup stopped because process ownership became ambiguous."
        case .cleanupIncomplete: return "Runtime cleanup did not fully terminate the owned process tree."
        case .metadata(let message): return "Ownership metadata is unavailable: \(message)"
        case .operationFailed(let message): return message
        }
    }
}

public protocol RuntimeBackend: AnyObject {
    func observeStatus() throws -> RuntimeStatus
    func startOwned() throws
    func stopOwned() throws
}

public final class RuntimeController: @unchecked Sendable {
    private let backend: RuntimeBackend

    public init(backend: RuntimeBackend) {
        self.backend = backend
    }

    public func refresh() -> RuntimeStatus {
        do {
            return try backend.observeStatus()
        } catch {
            return .ambiguous(error.localizedDescription)
        }
    }

    public func start() -> Result<RuntimeStatus, RuntimeLifecycleError> {
        perform(.start)
    }

    public func stop() -> Result<RuntimeStatus, RuntimeLifecycleError> {
        perform(.stop)
    }

    public func restart() -> Result<RuntimeStatus, RuntimeLifecycleError> {
        perform(.restart)
    }

    private func perform(_ action: RuntimeAction) -> Result<RuntimeStatus, RuntimeLifecycleError> {
        do {
            let current = try backend.observeStatus()
            let availability = RuntimePolicy.actions(for: current)
            switch action {
            case .start:
                guard availability.canStart else {
                    throw RuntimeLifecycleError.actionUnavailable("Start is unavailable for the current Runtime state.")
                }
                try backend.startOwned()
            case .stop:
                guard availability.canStop else {
                    throw RuntimeLifecycleError.actionUnavailable("Stop is available only for a positively app-owned Runtime.")
                }
                try backend.stopOwned()
            case .restart:
                guard availability.canRestart else {
                    throw RuntimeLifecycleError.actionUnavailable("Restart is available only for a positively app-owned Runtime.")
                }
                try backend.stopOwned()
                try backend.startOwned()
            }
            return .success(refresh())
        } catch let error as RuntimeLifecycleError {
            return .failure(error)
        } catch {
            return .failure(.operationFailed(error.localizedDescription))
        }
    }
}
