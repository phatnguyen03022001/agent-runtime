import Foundation
import XCTest
@testable import AgentRuntimeCore

final class ProtectedRuntimeTests: XCTestCase {
    func testProtectionAuditReaderReturnsBoundedOperatorVisibleSummary() throws {
        let root = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent("agent-runtime-audit-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let file = root.appendingPathComponent("protected-attempts.json")
        try Data(#"{"version":1,"blocked_count":7,"events":[{"at":"2026-09-11T12:00:00Z","category":"canonical_process_signal","tool":"terminal_exec"}]}"#.utf8).write(to: file)

        let snapshot = ProtectionAuditReader(url: file).read()

        XCTAssertEqual(snapshot.blockedCount, 7)
        XCTAssertEqual(snapshot.lastCategory, "canonical_process_signal")
        XCTAssertEqual(snapshot.lastAt, "2026-09-11T12:00:00Z")
    }

    func testNativeBackendUsesExplicitLifecycleScriptActionsAndDesiredState() throws {
        let root = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent("agent-runtime-backend-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let log = root.appendingPathComponent("actions.log")
        let desired = root.appendingPathComponent("protected-runtime-running")
        let script = root.appendingPathComponent("start.sh")
        try """
        #!/bin/bash
        set -e
        echo "$1" >> "\(log.path)"
        case "$1" in
          start|restart) touch "\(desired.path)" ;;
          stop) rm -f "\(desired.path)" ;;
        esac
        """.write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: script.path)

        let identity = ProcessIdentity(pid: 501, processGroupID: 501, startSeconds: 1, startMicroseconds: 1, executablePath: "/opt/homebrew/bin/tunnel-client")
        let inspector = FixtureInspector(identity: identity)
        let discovery = MutableDiscovery()
        let backend = NativeRuntimeBackend(
            configuration: RuntimeConfiguration(checkoutRoot: root.path, desiredStateURL: desired, transitionTimeout: 0.2),
            inspector: inspector,
            discovery: discovery
        )

        discovery.pids = [501]
        try backend.startOwned()
        XCTAssertEqual(try backend.observeStatus(), .owned(identity))
        try backend.restartOwned()
        XCTAssertEqual(try backend.observeStatus(), .owned(identity))
        discovery.pids = []
        try backend.stopOwned()
        XCTAssertEqual(try backend.observeStatus(), .stopped)
        XCTAssertEqual(try String(contentsOf: log).split(separator: "\n").map(String.init), ["start", "restart", "stop"])
    }

    func testDesiredStoppedCanonicalProcessRemainsReadOnlyExternal() throws {
        let identity = ProcessIdentity(pid: 601, processGroupID: 601, startSeconds: 1, startMicroseconds: 1, executablePath: "/opt/homebrew/bin/tunnel-client")
        let discovery = MutableDiscovery(); discovery.pids = [601]
        let desired = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("missing-\(UUID().uuidString)")
        let backend = NativeRuntimeBackend(
            configuration: RuntimeConfiguration(checkoutRoot: "/tmp", desiredStateURL: desired, transitionTimeout: 0),
            inspector: FixtureInspector(identity: identity),
            discovery: discovery
        )
        XCTAssertEqual(try backend.observeStatus(), .external([601]))
    }
}

private final class MutableDiscovery: RuntimeDiscovering {
    var pids: [Int32] = []
    func matchingRuntimePIDs(checkoutRoot: String) throws -> [Int32] { pids }
}

private final class FixtureInspector: ProcessInspecting {
    let identity: ProcessIdentity
    init(identity: ProcessIdentity) { self.identity = identity }
    func snapshot(pid: Int32) -> ProcessIdentity? { pid == identity.pid ? identity : nil }
    func groupMembers(processGroupID: Int32) -> [ProcessIdentity] { [] }
}
