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
            configuration: RuntimeConfiguration(checkoutRoot: "/tmp/fixture", profile: "fixture"),
            inspector: inspector,
            store: store,
            supervisor: supervisor,
            discovery: FakeDiscovery(pids: [])
        )

        XCTAssertEqual(try backend.observeStatus(), .owned(current))
        XCTAssertTrue(signaler.signals.isEmpty)
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
            configuration: RuntimeConfiguration(checkoutRoot: "/tmp/fixture", profile: "fixture"),
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
    func matchingRuntimePIDs(profile: String) throws -> [Int32] { pids }
}
