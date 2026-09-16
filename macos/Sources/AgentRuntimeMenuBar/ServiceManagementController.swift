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

    var errorDescription: String? {
        switch self {
        case .serviceNotFound(let name):
            return "ServiceManagement metadata was not found for \(name)."
        case .rollbackFailed(let detail):
            return "ServiceManagement rollback failed: \(detail)"
        }
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
    static let runtimePlistName = "com.picmao.agent-runtime-runtime.plist"

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
        let mainCreated = try ensureRegistered(mainApp, name: "main app")
        do {
            _ = try ensureRegistered(runtimeAgent, name: "Runtime LaunchAgent")
        } catch {
            if mainCreated {
                do {
                    try mainApp.unregister()
                } catch let rollbackError {
                    throw ServiceRegistrationError.rollbackFailed(rollbackError.localizedDescription)
                }
            }
            throw error
        }
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

    private func ensureRegistered(_ service: ServiceControlling, name: String) throws -> Bool {
        switch service.status {
        case .enabled, .requiresApproval:
            return false
        case .notRegistered:
            try service.register()
            return true
        case .notFound:
            throw ServiceRegistrationError.serviceNotFound(name)
        }
    }

    private func ensureUnregistered(_ service: ServiceControlling, name: String) throws -> Bool {
        switch service.status {
        case .enabled, .requiresApproval:
            try service.unregister()
            return true
        case .notRegistered:
            return false
        case .notFound:
            throw ServiceRegistrationError.serviceNotFound(name)
        }
    }
}

enum ServiceManagementCommand {
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
            case "unregister": snapshot = try coordinator.unregister()
            default:
                fputs("SERVICE MANAGEMENT ERROR: expected status, register, or unregister\n", stderr)
                return 2
            }
            let data = try snapshot.jsonData()
            FileHandle.standardOutput.write(data)
            FileHandle.standardOutput.write(Data("\n".utf8))
            return 0
        } catch {
            fputs("SERVICE MANAGEMENT ERROR: \(error.localizedDescription)\n", stderr)
            return 2
        }
    }
}
