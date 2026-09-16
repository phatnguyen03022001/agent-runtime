@testable import AgentRuntimeMenuBar
import XCTest

final class ServiceManagementTests: XCTestCase {
    func testStatusSnapshotPreservesAllFourOperatorVisibleStates() throws {
        let main = FakeService(status: .enabled)
        let runtime = FakeService(status: .requiresApproval)
        let coordinator = ServiceRegistrationCoordinator(mainApp: main, runtimeAgent: runtime)

        XCTAssertEqual(
            coordinator.snapshot(),
            ServiceRegistrationSnapshot(mainApp: .enabled, runtimeAgent: .requiresApproval)
        )
        XCTAssertEqual(ServiceRegistrationState.notRegistered.rawValue, "not-registered")
        XCTAssertEqual(ServiceRegistrationState.notFound.rawValue, "not-found")
    }

    func testRegisterAttemptsNotFoundServicesInsteadOfRejectingLocally() throws {
        let main = FakeService(status: .notFound)
        let runtime = FakeService(status: .notRegistered)
        let coordinator = ServiceRegistrationCoordinator(mainApp: main, runtimeAgent: runtime)

        let snapshot = try coordinator.register()

        XCTAssertEqual(snapshot, ServiceRegistrationSnapshot(mainApp: .enabled, runtimeAgent: .enabled))
        XCTAssertEqual(main.registerCount, 1)
        XCTAssertEqual(runtime.registerCount, 1)
    }

    func testRegisterAttemptsNotFoundRuntimeAgent() throws {
        let main = FakeService(status: .enabled)
        let runtime = FakeService(status: .notFound)
        let coordinator = ServiceRegistrationCoordinator(mainApp: main, runtimeAgent: runtime)

        let snapshot = try coordinator.register()

        XCTAssertEqual(snapshot, ServiceRegistrationSnapshot(mainApp: .enabled, runtimeAgent: .enabled))
        XCTAssertEqual(main.registerCount, 0)
        XCTAssertEqual(runtime.registerCount, 1)
    }

    func testMainRegistrationFailureDoesNotDisturbPreexistingRuntimeOwnership() throws {
        let main = FakeService(status: .notRegistered, registerError: FixtureError.failed)
        let runtime = FakeService(status: .enabled)
        let coordinator = ServiceRegistrationCoordinator(mainApp: main, runtimeAgent: runtime)

        XCTAssertThrowsError(try coordinator.register())
        XCTAssertEqual(main.registerCount, 1)
        XCTAssertEqual(runtime.registerCount, 0)
        XCTAssertEqual(runtime.unregisterCount, 0)
        XCTAssertEqual(runtime.status, .enabled)
    }

    func testMainRegistrationFailureStillAttemptsRuntimeAndCompensatesOnlyNewRuntimeState() throws {
        let main = FakeService(status: .notRegistered, registerError: FixtureError.failed)
        let runtime = FakeService(status: .notRegistered)
        let coordinator = ServiceRegistrationCoordinator(mainApp: main, runtimeAgent: runtime)

        XCTAssertThrowsError(try coordinator.register())
        XCTAssertEqual(main.registerCount, 1)
        XCTAssertEqual(runtime.registerCount, 1)
        XCTAssertEqual(runtime.unregisterCount, 1)
        XCTAssertEqual(runtime.status, .notRegistered)
    }

    func testRegisterIsIdempotentAndRollsBackOnlyRegistrationItCreated() throws {
        let main = FakeService(status: .notRegistered)
        let runtime = FakeService(status: .notRegistered, registerError: FixtureError.failed)
        let coordinator = ServiceRegistrationCoordinator(mainApp: main, runtimeAgent: runtime)

        XCTAssertThrowsError(try coordinator.register())
        XCTAssertEqual(main.registerCount, 1)
        XCTAssertEqual(main.unregisterCount, 1)
        XCTAssertEqual(runtime.registerCount, 1)

        let existingMain = FakeService(status: .enabled)
        let failingRuntime = FakeService(status: .notRegistered, registerError: FixtureError.failed)
        let second = ServiceRegistrationCoordinator(mainApp: existingMain, runtimeAgent: failingRuntime)
        XCTAssertThrowsError(try second.register())
        XCTAssertEqual(existingMain.unregisterCount, 0)
    }


    func testRegisterRuntimeFromNotRegisteredReportsCreatedEnabledState() throws {
        let main = FakeService(status: .enabled)
        let runtime = FakeService(status: .notRegistered)
        let coordinator = ServiceRegistrationCoordinator(mainApp: main, runtimeAgent: runtime)

        let snapshot = try coordinator.registerRuntimeAgent()

        XCTAssertEqual(snapshot, ServiceRegistrationSnapshot(mainApp: .enabled, runtimeAgent: .enabled))
        XCTAssertEqual(runtime.registerCount, 1)
    }

    func testRegisterErrorReconcilesNotRegisteredToRequiresApproval() throws {
        let main = FakeService(status: .enabled)
        let runtime = FakeService(
            status: .notRegistered,
            registerError: FixtureError.failed,
            statusAfterRegisterError: .requiresApproval
        )
        let coordinator = ServiceRegistrationCoordinator(mainApp: main, runtimeAgent: runtime)

        let snapshot = try coordinator.registerRuntimeAgent()

        XCTAssertEqual(snapshot, ServiceRegistrationSnapshot(mainApp: .enabled, runtimeAgent: .requiresApproval))
        XCTAssertEqual(runtime.registerCount, 1)
    }

    func testRegisterErrorReconcilesNotFoundToRequiresApproval() throws {
        let main = FakeService(status: .enabled)
        let runtime = FakeService(
            status: .notFound,
            registerError: FixtureError.failed,
            statusAfterRegisterError: .requiresApproval
        )
        let coordinator = ServiceRegistrationCoordinator(mainApp: main, runtimeAgent: runtime)

        let snapshot = try coordinator.registerRuntimeAgent()

        XCTAssertEqual(snapshot, ServiceRegistrationSnapshot(mainApp: .enabled, runtimeAgent: .requiresApproval))
        XCTAssertEqual(runtime.registerCount, 1)
    }

    func testRegisterErrorStillPropagatesWhenStatusRemainsNotRegistered() throws {
        let main = FakeService(status: .enabled)
        let runtime = FakeService(status: .notRegistered, registerError: FixtureError.failed)
        let coordinator = ServiceRegistrationCoordinator(mainApp: main, runtimeAgent: runtime)

        XCTAssertThrowsError(try coordinator.registerRuntimeAgent()) { error in
            guard case FixtureError.failed = error else {
                return XCTFail("expected original FixtureError.failed, got \(error)")
            }
        }
        XCTAssertEqual(runtime.registerCount, 1)
        XCTAssertEqual(runtime.status, .notRegistered)
    }

    func testRegisterErrorStillPropagatesWhenStatusRemainsNotFound() throws {
        let main = FakeService(status: .enabled)
        let runtime = FakeService(status: .notFound, registerError: FixtureError.failed)
        let coordinator = ServiceRegistrationCoordinator(mainApp: main, runtimeAgent: runtime)

        XCTAssertThrowsError(try coordinator.registerRuntimeAgent()) { error in
            guard case FixtureError.failed = error else {
                return XCTFail("expected original FixtureError.failed, got \(error)")
            }
        }
        XCTAssertEqual(runtime.registerCount, 1)
        XCTAssertEqual(runtime.status, .notFound)
    }

    func testRegisterErrorStillPropagatesWhenPostErrorStatusIsEnabled() throws {
        let main = FakeService(status: .enabled)
        let runtime = FakeService(
            status: .notRegistered,
            registerError: FixtureError.failed,
            statusAfterRegisterError: .enabled
        )
        let coordinator = ServiceRegistrationCoordinator(mainApp: main, runtimeAgent: runtime)

        XCTAssertThrowsError(try coordinator.registerRuntimeAgent()) { error in
            guard case FixtureError.failed = error else {
                return XCTFail("expected original FixtureError.failed, got \(error)")
            }
        }
        XCTAssertEqual(runtime.registerCount, 1)
        XCTAssertEqual(runtime.status, .enabled)
    }

    func testExistingRequiresApprovalDoesNotRegisterAgain() throws {
        let main = FakeService(status: .enabled)
        let runtime = FakeService(status: .requiresApproval, registerError: FixtureError.failed)
        let coordinator = ServiceRegistrationCoordinator(mainApp: main, runtimeAgent: runtime)

        let snapshot = try coordinator.registerRuntimeAgent()

        XCTAssertEqual(snapshot, ServiceRegistrationSnapshot(mainApp: .enabled, runtimeAgent: .requiresApproval))
        XCTAssertEqual(runtime.registerCount, 0)
    }

    func testApprovalCreatedRuntimeRegistrationIsCompensatedWhenEarlierAggregateStepFailed() throws {
        let main = FakeService(status: .notRegistered, registerError: FixtureError.failed)
        let runtime = FakeService(
            status: .notRegistered,
            registerError: FixtureError.failed,
            statusAfterRegisterError: .requiresApproval
        )
        let coordinator = ServiceRegistrationCoordinator(mainApp: main, runtimeAgent: runtime)

        XCTAssertThrowsError(try coordinator.register()) { error in
            guard case FixtureError.failed = error else {
                return XCTFail("expected original FixtureError.failed, got \(error)")
            }
        }
        XCTAssertEqual(main.registerCount, 1)
        XCTAssertEqual(runtime.registerCount, 1)
        XCTAssertEqual(runtime.unregisterCount, 1)
        XCTAssertEqual(runtime.status, .notRegistered)
    }

    func testUnregisterCompensatesRuntimeWhenMainUnregisterFails() throws {
        let main = FakeService(status: .enabled, unregisterError: FixtureError.failed)
        let runtime = FakeService(status: .enabled)
        let coordinator = ServiceRegistrationCoordinator(mainApp: main, runtimeAgent: runtime)

        XCTAssertThrowsError(try coordinator.unregister())
        XCTAssertEqual(runtime.unregisterCount, 1)
        XCTAssertEqual(runtime.registerCount, 1)
        XCTAssertEqual(runtime.status, .enabled)
        XCTAssertEqual(main.status, .enabled)
    }

    func testUnregisterReportsRollbackFailureWhenCompensationFails() throws {
        let main = FakeService(status: .enabled, unregisterError: FixtureError.failed)
        let runtime = FakeService(status: .enabled, registerError: FixtureError.failed)
        let coordinator = ServiceRegistrationCoordinator(mainApp: main, runtimeAgent: runtime)

        XCTAssertThrowsError(try coordinator.unregister()) { error in
            guard case ServiceRegistrationError.rollbackFailed = error else {
                return XCTFail("expected rollbackFailed, got \(error)")
            }
        }
        XCTAssertEqual(runtime.status, .notRegistered)
        XCTAssertEqual(main.status, .enabled)
    }
    func testUnregisterTreatsNotFoundAsAbsentWithoutCallingUnregister() throws {
        let main = FakeService(status: .notFound)
        let runtime = FakeService(status: .enabled)
        let coordinator = ServiceRegistrationCoordinator(mainApp: main, runtimeAgent: runtime)

        let snapshot = try coordinator.unregister()

        XCTAssertEqual(snapshot, ServiceRegistrationSnapshot(mainApp: .notFound, runtimeAgent: .notRegistered))
        XCTAssertEqual(main.unregisterCount, 0)
        XCTAssertEqual(runtime.unregisterCount, 1)
    }

    func testUnregisterLeavesBothServicesNotRegistered() throws {
        let main = FakeService(status: .enabled)
        let runtime = FakeService(status: .requiresApproval)
        let coordinator = ServiceRegistrationCoordinator(mainApp: main, runtimeAgent: runtime)

        let snapshot = try coordinator.unregister()
        XCTAssertEqual(snapshot, ServiceRegistrationSnapshot(mainApp: .notRegistered, runtimeAgent: .notRegistered))
        XCTAssertEqual(runtime.unregisterCount, 1)
        XCTAssertEqual(main.unregisterCount, 1)
    }
}

private enum FixtureError: Error { case failed }

private final class FakeService: ServiceControlling {
    var status: ServiceRegistrationState
    var registerError: Error?
    var statusAfterRegisterError: ServiceRegistrationState?
    var unregisterError: Error?
    var registerCount = 0
    var unregisterCount = 0

    init(
        status: ServiceRegistrationState,
        registerError: Error? = nil,
        statusAfterRegisterError: ServiceRegistrationState? = nil,
        unregisterError: Error? = nil
    ) {
        self.status = status
        self.registerError = registerError
        self.statusAfterRegisterError = statusAfterRegisterError
        self.unregisterError = unregisterError
    }

    func register() throws {
        registerCount += 1
        if let registerError {
            if let statusAfterRegisterError { status = statusAfterRegisterError }
            throw registerError
        }
        status = .enabled
    }

    func unregister() throws {
        unregisterCount += 1
        if let unregisterError { throw unregisterError }
        status = .notRegistered
    }
}
