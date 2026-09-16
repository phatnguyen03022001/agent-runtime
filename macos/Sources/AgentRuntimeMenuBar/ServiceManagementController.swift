import Foundation
import ServiceManagement

enum ServiceRegistrationState: String, Codable, Equatable, Sendable {
    case enabled
    case requiresApproval = "requires-approval"
    case notRegistered = "not-registered"
    case notFound = "not-found"
}

struct ServiceRegistrationSnapshot: Codable, Equatable, Sendable {
    let mainApp: ServiceRegistrationState
    let runtimeAgent: ServiceRegistrationState

    func jsonData() throws -> Data {
        try JSONSerialization.data(
            withJSONObject: [
                "main_app": mainApp.rawValue,
                "runtime_agent": runtimeAgent.rawValue,
            ],
            options: [.sortedKeys]
        )
    }
}

protocol ServiceControlling: AnyObject {
    var status: ServiceRegistrationState { get }
    func register() throws
    func unregister() throws
}

enum ServiceRegistrationError: Error, LocalizedError {
    case serviceNotFound(String)
    case rollbackFailed(String)
    case approvalRequired

    var errorDescription: String? {
        switch self {
        case .serviceNotFound(let name):
            return "ServiceManagement metadata was not found for \(name)."
        case .rollbackFailed(let detail):
            return "ServiceManagement rollback failed: \(detail)"
        case .approvalRequired:
            return "ServiceManagement registration requires explicit user approval."
        }
    }

    static func isApprovalRequired(_ error: Error) -> Bool {
        let value = error as NSError
        if #available(macOS 15.0, *) {
            return value.domain == SMAppServiceErrorDomain && value.code == 1
        }
        return false
    }
}

final class SMAppServiceControl: ServiceControlling {
    private let service: SMAppService

    init(service: SMAppService) {
        self.service = service
    }

    var status: ServiceRegistrationState {
        switch service.status {
        case .enabled: return .enabled
        case .requiresApproval: return .requiresApproval
        case .notRegistered: return .notRegistered
        case .notFound: return .notFound
        @unknown default: return .notFound
        }
    }

    func register() throws {
        try service.register()
    }

    func unregister() throws {
        try service.unregister()
    }
}

final class ServiceRegistrationCoordinator {
    static let runtimePlistName = "com.picmao.agent-runtime-runtime-service.plist"

    private let mainApp: ServiceControlling
    private let runtimeAgent: ServiceControlling

    init(mainApp: ServiceControlling, runtimeAgent: ServiceControlling) {
        self.mainApp = mainApp
        self.runtimeAgent = runtimeAgent
    }

    static func production() -> ServiceRegistrationCoordinator {
        ServiceRegistrationCoordinator(
            mainApp: SMAppServiceControl(service: .mainApp),
            runtimeAgent: SMAppServiceControl(service: .agent(plistName: runtimePlistName))
        )
    }

    func snapshot() -> ServiceRegistrationSnapshot {
        ServiceRegistrationSnapshot(mainApp: mainApp.status, runtimeAgent: runtimeAgent.status)
    }

    @discardableResult
    func register() throws -> ServiceRegistrationSnapshot {
        var mainCreated = false
        var runtimeCreated = false
        var firstError: Error?

        do {
            mainCreated = try ensureRegistered(mainApp, name: "main app")
        } catch {
            firstError = error
        }

        do {
            runtimeCreated = try ensureRegistered(runtimeAgent, name: "Runtime LaunchAgent")
        } catch {
            if firstError == nil {
                firstError = error
            }
        }

        if let firstError {
            try compensateCreatedRegistrations(mainCreated: mainCreated, runtimeCreated: runtimeCreated)
            throw firstError
        }
        return snapshot()
    }

    @discardableResult
    func registerMainApp() throws -> ServiceRegistrationSnapshot {
        _ = try ensureRegistered(mainApp, name: "main app")
        return snapshot()
    }

    @discardableResult
    func registerRuntimeAgent() throws -> ServiceRegistrationSnapshot {
        _ = try ensureRegistered(runtimeAgent, name: "Runtime LaunchAgent")
        return snapshot()
    }

    @discardableResult
    func unregister() throws -> ServiceRegistrationSnapshot {
        let runtimeRemoved = try ensureUnregistered(runtimeAgent, name: "Runtime LaunchAgent")
        do {
            _ = try ensureUnregistered(mainApp, name: "main app")
        } catch {
            if runtimeRemoved {
                do {
                    _ = try ensureRegistered(runtimeAgent, name: "Runtime LaunchAgent")
                } catch let rollbackError {
                    throw ServiceRegistrationError.rollbackFailed(rollbackError.localizedDescription)
                }
            }
            throw error
        }
        return snapshot()
    }

    @discardableResult
    func unregisterMainApp() throws -> ServiceRegistrationSnapshot {
        _ = try ensureUnregistered(mainApp, name: "main app")
        return snapshot()
    }

    @discardableResult
    func unregisterRuntimeAgent() throws -> ServiceRegistrationSnapshot {
        _ = try ensureUnregistered(runtimeAgent, name: "Runtime LaunchAgent")
        return snapshot()
    }

    private func compensateCreatedRegistrations(mainCreated: Bool, runtimeCreated: Bool) throws {
        var failures: [String] = []
        if runtimeCreated {
            do {
                try runtimeAgent.unregister()
            } catch {
                failures.append("Runtime LaunchAgent: \(error.localizedDescription)")
            }
        }
        if mainCreated {
            do {
                try mainApp.unregister()
            } catch {
                failures.append("main app: \(error.localizedDescription)")
            }
        }
        if !failures.isEmpty {
            throw ServiceRegistrationError.rollbackFailed(failures.joined(separator: "; "))
        }
    }

    private func ensureRegistered(_ service: ServiceControlling, name: String) throws -> Bool {
        switch service.status {
        case .enabled, .requiresApproval:
            return false
        case .notRegistered, .notFound:
            do {
                try service.register()
                return true
            } catch {
                let postErrorStatus = service.status
                if postErrorStatus == .requiresApproval {
                    return true
                }
                if ServiceRegistrationError.isApprovalRequired(error),
                   postErrorStatus == .notRegistered || postErrorStatus == .notFound {
                    throw ServiceRegistrationError.approvalRequired
                }
                throw error
            }
        }
    }

    private func ensureUnregistered(_ service: ServiceControlling, name: String) throws -> Bool {
        switch service.status {
        case .enabled, .requiresApproval:
            try service.unregister()
            return true
        case .notRegistered, .notFound:
            return false
        }
    }
}

enum ServiceManagementCommand {
    static let approvalRequiredExitStatus: Int32 = 3

    static func approvalRequiredJSONData(
        operation: String,
        snapshot: ServiceRegistrationSnapshot
    ) throws -> Data {
        try JSONSerialization.data(
            withJSONObject: [
                "main_app": snapshot.mainApp.rawValue,
                "runtime_agent": snapshot.runtimeAgent.rawValue,
                "operation": operation,
                "outcome": "approval-required",
            ],
            options: [.sortedKeys]
        )
    }

    static func run(arguments: [String]) -> Int32? {
        guard arguments.count >= 3, arguments[1] == "--service-management" else {
            return nil
        }
        let coordinator = ServiceRegistrationCoordinator.production()
        do {
            let snapshot: ServiceRegistrationSnapshot
            switch arguments[2] {
            case "status": snapshot = coordinator.snapshot()
            case "register": snapshot = try coordinator.register()
            case "register-main": snapshot = try coordinator.registerMainApp()
            case "register-runtime": snapshot = try coordinator.registerRuntimeAgent()
            case "unregister": snapshot = try coordinator.unregister()
            case "unregister-main": snapshot = try coordinator.unregisterMainApp()
            case "unregister-runtime": snapshot = try coordinator.unregisterRuntimeAgent()
            default:
                fputs("SERVICE MANAGEMENT ERROR: expected status/register/unregister operation\n", stderr)
                return 2
            }
            let data = try snapshot.jsonData()
            FileHandle.standardOutput.write(data)
            FileHandle.standardOutput.write(Data("\n".utf8))
            return 0
        } catch ServiceRegistrationError.approvalRequired {
            do {
                let data = try approvalRequiredJSONData(
                    operation: arguments[2],
                    snapshot: coordinator.snapshot()
                )
                FileHandle.standardOutput.write(data)
                FileHandle.standardOutput.write(Data("\n".utf8))
                return approvalRequiredExitStatus
            } catch {
                fputs("SERVICE MANAGEMENT ERROR: could not encode approval-required result\n", stderr)
                return 2
            }
        } catch {
            fputs("SERVICE MANAGEMENT ERROR: \(error.localizedDescription)\n", stderr)
            return 2
        }
    }
}
