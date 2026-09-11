import XCTest
@testable import AgentRuntimeCore

final class RuntimeLifecycleTests: XCTestCase {
    private let ownedIdentity = ProcessIdentity(
        pid: 101,
        processGroupID: 101,
        startSeconds: 10,
        startMicroseconds: 20,
        executablePath: "/tmp/tunnel-client"
    )

    func testRefreshNeverMutatesLifecycle() {
        let backend = FakeBackend(state: .stopped, ownedIdentity: ownedIdentity)
        let controller = RuntimeController(backend: backend)

        XCTAssertEqual(controller.refresh(), .stopped)
        XCTAssertEqual(controller.refresh(), .stopped)
        XCTAssertEqual(backend.startCount, 0)
        XCTAssertEqual(backend.stopCount, 0)
    }

    func testExternalRuntimeIsReadOnlyForEveryLifecycleAction() {
        let backend = FakeBackend(state: .external([9001]), ownedIdentity: ownedIdentity)
        let controller = RuntimeController(backend: backend)

        assertFailure(controller.start())
        assertFailure(controller.stop())
        assertFailure(controller.restart())
        XCTAssertEqual(backend.startCount, 0)
        XCTAssertEqual(backend.stopCount, 0)
    }

    func testOnlyExplicitStartMutatesStoppedRuntime() {
        let backend = FakeBackend(state: .stopped, ownedIdentity: ownedIdentity)
        let controller = RuntimeController(backend: backend)

        XCTAssertEqual(controller.refresh(), .stopped)
        XCTAssertEqual(backend.startCount, 0)
        if case .success(.owned) = controller.start() {} else { XCTFail("explicit Start should own Runtime") }
        XCTAssertEqual(backend.startCount, 1)
        XCTAssertEqual(backend.stopCount, 0)
    }

    func testRestartRequiresOwnedRuntime() {
        let backend = FakeBackend(state: .owned(ownedIdentity), ownedIdentity: ownedIdentity)
        let controller = RuntimeController(backend: backend)

        if case .success(.owned) = controller.restart() {} else { XCTFail("owned Runtime should restart") }
        XCTAssertEqual(backend.stopCount, 1)
        XCTAssertEqual(backend.startCount, 1)
    }

    private func assertFailure(_ result: Result<RuntimeStatus, RuntimeLifecycleError>) {
        if case .success = result { XCTFail("action must fail closed") }
    }
}

private final class FakeBackend: RuntimeBackend {
    var state: RuntimeStatus
    let ownedIdentity: ProcessIdentity
    var startCount = 0
    var stopCount = 0

    init(state: RuntimeStatus, ownedIdentity: ProcessIdentity) {
        self.state = state
        self.ownedIdentity = ownedIdentity
    }

    func observeStatus() throws -> RuntimeStatus { state }

    func startOwned() throws {
        startCount += 1
        state = .owned(ownedIdentity)
    }

    func stopOwned() throws {
        stopCount += 1
        state = .stopped
    }
}
