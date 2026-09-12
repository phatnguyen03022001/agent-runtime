import Darwin
import Foundation
import XCTest
@testable import AgentRuntimeCore

final class OwnershipTests: XCTestCase {
    func testPIDReuseEquivalentFailsRecordMatch() {
        let original = identity(pid: 777, seconds: 100)
        let reused = identity(pid: 777, seconds: 101)
        let record = OwnershipRecord(rootProcess: original, profile: "fixture", checkoutRoot: "/tmp/fixture")

        XCTAssertFalse(record.matches(reused))
    }

    func testStaleOwnershipNeverSignalsReusedPID() throws {
        let original = identity(pid: 777, seconds: 100)
        let reused = identity(pid: 777, seconds: 101)
        let store = MemoryStore(record: OwnershipRecord(rootProcess: original, profile: "fixture", checkoutRoot: "/tmp/fixture"))
        let inspector = FakeInspector(snapshot: reused)
        let signaler = RecordingSignaler()
        let supervisor = OwnedProcessSupervisor(
            inspector: inspector,
            signaler: signaler,
            store: store,
            launcher: FailingLauncher()
        )

        XCTAssertThrowsError(try supervisor.stopOwned()) { error in
            XCTAssertEqual(error as? RuntimeLifecycleError, .ownershipMismatch)
        }
        XCTAssertTrue(signaler.signals.isEmpty)
        XCTAssertNotNil(store.record)
    }

    func testPersistedMatchingIdentityRevalidatesAsOwnedAfterRelaunch() throws {
        let current = identity(pid: 888, seconds: 200)
        let store = MemoryStore(
            record: OwnershipRecord(rootProcess: current, profile: "fixture", checkoutRoot: "/tmp/fixture")
        )
        let inspector = FakeInspector(snapshot: current)
        let signaler = RecordingSignaler()
        let supervisor = OwnedProcessSupervisor(
            inspector: inspector,
            signaler: signaler,
            store: store,
            launcher: FailingLauncher()
        )
        let backend = NativeRuntimeBackend(
            configuration: RuntimeConfiguration(checkoutRoot: "/tmp/fixture"),
            inspector: inspector,
            store: store,
            supervisor: supervisor,
            discovery: FakeDiscovery(pids: [])
        )

        XCTAssertEqual(try backend.observeStatus(), .owned(current))
        XCTAssertTrue(signaler.signals.isEmpty)
    }

    func testProductionDiscoveryRecognizesCanonicalEnvBackedInvocation() throws {
        let pid: Int32 = 9002
        let inspector = FakeInspector(snapshot: ProcessIdentity(
            pid: pid,
            processGroupID: pid,
            startSeconds: 1,
            startMicroseconds: 1,
            executablePath: "/opt/homebrew/bin/tunnel-client"
        ))
        let discovery = PSRuntimeDiscovery(
            inspector: inspector,
            processListProvider: { "\(pid) /opt/homebrew/bin/tunnel-client run --control-plane.poll-channel main --mcp.command command=/tmp/fixture/.venv/bin/python -m agent_runtime.server,channel=main --health.listen-addr 127.0.0.1:8080\n" }
        )

        XCTAssertEqual(try discovery.matchingRuntimePIDs(checkoutRoot: "/tmp/fixture"), [pid])
    }

    func testProductionDiscoveryDrainsLargeProcessOutputAndFollowingActionCompletes() throws {
        let root = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent("agent-runtime-large-discovery-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }

        let pid: Int32 = 9010
        let identity = ProcessIdentity(
            pid: pid,
            processGroupID: pid,
            startSeconds: 1,
            startMicroseconds: 1,
            executablePath: "/opt/homebrew/bin/tunnel-client"
        )
        let desired = root.appendingPathComponent("protected-runtime-running")
        FileManager.default.createFile(atPath: desired.path, contents: nil)
        let script = root.appendingPathComponent("start.sh")
        try """
        #!/bin/bash
        set -e
        case "$1" in
          restart) touch "\(desired.path)" ;;
          *) exit 2 ;;
        esac
        """.write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: script.path)

        let processListCommand = """
        /usr/bin/head -c 131072 /dev/zero
        printf '\\n\(pid) /opt/homebrew/bin/tunnel-client run --control-plane.poll-channel main --mcp.command command=\(root.path)/.venv/bin/python -m agent_runtime.server,channel=main --health.listen-addr 127.0.0.1:8080\\n'
        """
        let fixtureOutput = try PSRuntimeDiscovery.readProcessList(
            executableURL: URL(fileURLWithPath: "/bin/sh"),
            arguments: ["-c", processListCommand]
        )
        XCTAssertGreaterThanOrEqual(fixtureOutput.utf8.count, 131072)

        let inspector = FakeInspector(snapshot: identity)
        let discovery = PSRuntimeDiscovery(
            inspector: inspector,
            processListProvider: {
                try PSRuntimeDiscovery.readProcessList(
                    executableURL: URL(fileURLWithPath: "/bin/sh"),
                    arguments: ["-c", processListCommand]
                )
            }
        )
        let backend = NativeRuntimeBackend(
            configuration: RuntimeConfiguration(
                checkoutRoot: root.path,
                desiredStateURL: desired,
                transitionTimeout: 2
            ),
            inspector: inspector,
            discovery: discovery
        )
        let controller = RuntimeController(backend: backend)
        let queue = DispatchQueue(label: "agent-runtime.large-discovery-fixture")
        let finished = expectation(description: "large discovery refresh and restart finish")
        let resultBox = ResultBox<RuntimeStatus, RuntimeLifecycleError>()

        queue.async {
            _ = controller.refresh()
            resultBox.store(controller.restart())
            finished.fulfill()
        }

        wait(for: [finished], timeout: 5)
        XCTAssertEqual(resultBox.load(), .success(.owned(identity)))
    }

    func testProductionDiscoveryRejectsDifferentRuntimePath() throws {
        let pid: Int32 = 9003
        let inspector = FakeInspector(snapshot: ProcessIdentity(
            pid: pid,
            processGroupID: pid,
            startSeconds: 1,
            startMicroseconds: 1,
            executablePath: "/opt/homebrew/bin/tunnel-client"
        ))
        let discovery = PSRuntimeDiscovery(
            inspector: inspector,
            processListProvider: { "\(pid) /opt/homebrew/bin/tunnel-client run --control-plane.poll-channel main --mcp.command command=/tmp/other/.venv/bin/python -m agent_runtime.server,channel=main --health.listen-addr 127.0.0.1:8080\n" }
        )

        XCTAssertEqual(try discovery.matchingRuntimePIDs(checkoutRoot: "/tmp/fixture"), [])
    }

    func testProductionDiscoveryRejectsUnexpectedArguments() throws {
        let pid: Int32 = 9007
        let inspector = FakeInspector(snapshot: ProcessIdentity(
            pid: pid,
            processGroupID: pid,
            startSeconds: 1,
            startMicroseconds: 1,
            executablePath: "/opt/homebrew/bin/tunnel-client"
        ))
        let discovery = PSRuntimeDiscovery(
            inspector: inspector,
            processListProvider: { "\(pid) /opt/homebrew/bin/tunnel-client run --control-plane.poll-channel other --mcp.command command=/tmp/fixture/.venv/bin/python -m agent_runtime.server,channel=main --health.listen-addr 127.0.0.1:8080\n" }
        )

        XCTAssertEqual(try discovery.matchingRuntimePIDs(checkoutRoot: "/tmp/fixture"), [])
    }

    func testProductionDiscoveryRejectsUnrelatedTunnelClientCommand() throws {
        let pid: Int32 = 9004
        let inspector = FakeInspector(snapshot: ProcessIdentity(
            pid: pid,
            processGroupID: pid,
            startSeconds: 1,
            startMicroseconds: 1,
            executablePath: "/opt/homebrew/bin/tunnel-client"
        ))
        let discovery = PSRuntimeDiscovery(
            inspector: inspector,
            processListProvider: { "\(pid) /opt/homebrew/bin/tunnel-client doctor --control-plane.poll-channel main\n" }
        )

        XCTAssertEqual(try discovery.matchingRuntimePIDs(checkoutRoot: "/tmp/fixture"), [])
    }

    func testProductionDiscoveryRequiresTunnelClientExecutableIdentity() throws {
        let pid: Int32 = 9005
        let inspector = FakeInspector(snapshot: ProcessIdentity(
            pid: pid,
            processGroupID: pid,
            startSeconds: 1,
            startMicroseconds: 1,
            executablePath: "/usr/bin/python3"
        ))
        let discovery = PSRuntimeDiscovery(
            inspector: inspector,
            processListProvider: { "\(pid) /tmp/tunnel-client run --control-plane.poll-channel main --mcp.command command=/tmp/fixture/.venv/bin/python -m agent_runtime.server,channel=main --health.listen-addr 127.0.0.1:8080\n" }
        )

        XCTAssertEqual(try discovery.matchingRuntimePIDs(checkoutRoot: "/tmp/fixture"), [])
    }

    func testCanonicalExternalDiscoveryIsReadOnlyAndCannotDoubleStart() throws {
        let pid: Int32 = 9006
        let identity = ProcessIdentity(
            pid: pid,
            processGroupID: pid,
            startSeconds: 1,
            startMicroseconds: 1,
            executablePath: "/opt/homebrew/bin/tunnel-client"
        )
        let inspector = FakeInspector(snapshot: identity)
        let signaler = RecordingSignaler()
        let store = MemoryStore(record: nil)
        let supervisor = OwnedProcessSupervisor(
            inspector: inspector,
            signaler: signaler,
            store: store,
            launcher: FailingLauncher()
        )
        let discovery = PSRuntimeDiscovery(
            inspector: inspector,
            processListProvider: { "\(pid) /opt/homebrew/bin/tunnel-client run --control-plane.poll-channel main --mcp.command command=/tmp/fixture/.venv/bin/python -m agent_runtime.server,channel=main --health.listen-addr 127.0.0.1:8080\n" }
        )
        let backend = NativeRuntimeBackend(
            configuration: RuntimeConfiguration(checkoutRoot: "/tmp/fixture"),
            inspector: inspector,
            store: store,
            supervisor: supervisor,
            discovery: discovery
        )
        let controller = RuntimeController(backend: backend)

        XCTAssertEqual(controller.refresh(), .external([pid]))
        XCTAssertEqual(controller.start(), .failure(.actionUnavailable("Start is unavailable for the current Runtime state.")))
        XCTAssertEqual(controller.stop(), .failure(.actionUnavailable("Stop is available only for a positively app-owned Runtime.")))
        XCTAssertEqual(controller.restart(), .failure(.actionUnavailable("Restart is available only for a positively app-owned Runtime.")))
        XCTAssertTrue(signaler.signals.isEmpty)
    }

    func testProductionDiscoveryRejectsWrongHealthListener() throws {
        let pid: Int32 = 9001
        let inspector = FakeInspector(snapshot: ProcessIdentity(
            pid: pid,
            processGroupID: pid,
            startSeconds: 1,
            startMicroseconds: 1,
            executablePath: "/opt/homebrew/bin/tunnel-client"
        ))
        let discovery = PSRuntimeDiscovery(
            inspector: inspector,
            processListProvider: { "\(pid) /opt/homebrew/bin/tunnel-client run --control-plane.poll-channel main --mcp.command command=/tmp/fixture/.venv/bin/python -m agent_runtime.server,channel=main --health.listen-addr 127.0.0.1:9090\n" }
        )

        XCTAssertEqual(try discovery.matchingRuntimePIDs(checkoutRoot: "/tmp/fixture"), [])
    }

    func testStaleRecordWithObservedExternalRuntimeRemainsReadOnly() throws {
        let original = identity(pid: 777, seconds: 100)
        let reused = identity(pid: 777, seconds: 101)
        let store = MemoryStore(
            record: OwnershipRecord(rootProcess: original, profile: "fixture", checkoutRoot: "/tmp/fixture")
        )
        let inspector = FakeInspector(snapshot: reused)
        let signaler = RecordingSignaler()
        let supervisor = OwnedProcessSupervisor(
            inspector: inspector,
            signaler: signaler,
            store: store,
            launcher: FailingLauncher()
        )
        let backend = NativeRuntimeBackend(
            configuration: RuntimeConfiguration(checkoutRoot: "/tmp/fixture"),
            inspector: inspector,
            store: store,
            supervisor: supervisor,
            discovery: FakeDiscovery(pids: [9001])
        )
        let controller = RuntimeController(backend: backend)

        XCTAssertEqual(controller.refresh(), .external([9001]))
        if case .success = controller.stop() { XCTFail("external Runtime must remain read-only") }
        if case .success = controller.restart() { XCTFail("external Runtime must remain read-only") }
        XCTAssertTrue(signaler.signals.isEmpty)
        XCTAssertNotNil(store.record)
    }

    func testSyntheticOwnedProcessTreeIsFullyCleaned() throws {
        let temp = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent("agent-runtime-tests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: temp, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: temp) }

        let system = DarwinProcessSystem()
        let store = FileOwnershipStore(url: temp.appendingPathComponent("ownership.json"))
        let launcher = POSIXProcessLauncher(inspector: system, signaler: system)
        let supervisor = OwnedProcessSupervisor(
            inspector: system,
            signaler: system,
            store: store,
            launcher: launcher
        )
        let spec = LaunchSpec(
            executablePath: "/bin/bash",
            arguments: ["-c", "sleep 30 & wait"],
            environment: ["PATH": "/usr/bin:/bin", "HOME": temp.path]
        )

        let record = try supervisor.start(spec: spec, profile: "synthetic-fixture", checkoutRoot: temp.path)
        Thread.sleep(forTimeInterval: 0.25)
        XCTAssertGreaterThanOrEqual(system.groupMembers(processGroupID: record.rootProcess.processGroupID).count, 2)

        try supervisor.stopOwned()

        XCTAssertTrue(system.groupMembers(processGroupID: record.rootProcess.processGroupID).isEmpty)
        XCTAssertFalse(FileManager.default.fileExists(atPath: store.url.path))
    }

    private func identity(pid: Int32, seconds: Int64) -> ProcessIdentity {
        ProcessIdentity(
            pid: pid,
            processGroupID: pid,
            startSeconds: seconds,
            startMicroseconds: 5,
            executablePath: "/tmp/tunnel-client"
        )
    }
}

private final class MemoryStore: OwnershipStoring {
    var record: OwnershipRecord?
    init(record: OwnershipRecord?) { self.record = record }
    func load() throws -> OwnershipRecord? { record }
    func save(_ record: OwnershipRecord) throws { self.record = record }
    func clear() throws { record = nil }
}

private final class FakeInspector: ProcessInspecting {
    var current: ProcessIdentity?
    init(snapshot: ProcessIdentity?) { self.current = snapshot }
    func snapshot(pid: Int32) -> ProcessIdentity? { current?.pid == pid ? current : nil }
    func groupMembers(processGroupID: Int32) -> [ProcessIdentity] {
        guard let current, current.processGroupID == processGroupID else { return [] }
        return [current]
    }
}

private final class ResultBox<Value: Sendable, Failure: Error & Sendable>: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Result<Value, Failure>?

    func store(_ value: Result<Value, Failure>) {
        lock.lock()
        self.value = value
        lock.unlock()
    }

    func load() -> Result<Value, Failure>? {
        lock.lock()
        defer { lock.unlock() }
        return value
    }
}

private final class RecordingSignaler: ProcessSignaling {
    var signals: [(String, Int32, Int32)] = []
    func signalProcessGroup(_ processGroupID: Int32, signal: Int32) -> Int32 {
        signals.append(("group", processGroupID, signal)); return 0
    }
    func signalProcess(_ pid: Int32, signal: Int32) -> Int32 {
        signals.append(("pid", pid, signal)); return 0
    }
}

private final class FailingLauncher: ProcessLaunching {
    func launchGroup(_ spec: LaunchSpec) throws -> ProcessIdentity {
        throw RuntimeLifecycleError.processLaunchFailed("not used")
    }
}

private final class FakeDiscovery: RuntimeDiscovering {
    let pids: [Int32]
    init(pids: [Int32]) { self.pids = pids }
    func matchingRuntimePIDs(checkoutRoot: String) throws -> [Int32] { pids }
}
