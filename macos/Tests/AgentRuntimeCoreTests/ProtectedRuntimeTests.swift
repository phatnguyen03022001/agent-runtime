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

        XCTAssertEqual(snapshot.blockedCount, 1)
        XCTAssertEqual(snapshot.lastCategory, "canonical_process_signal")
        XCTAssertEqual(snapshot.lastAt, "2026-09-11T12:00:00Z")
    }

    func testProtectionAuditReaderCapsRetainedHistoryAtTwentyEvents() throws {
        let root = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent("agent-runtime-audit-rollover-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let file = root.appendingPathComponent("protected-attempts.json")
        let events = (0..<25).map { index in
            [
                "at": "2026-09-13T00:00:\(String(format: "%02d", index))Z",
                "category": "category-\(index)",
                "tool": "terminal_exec",
            ]
        }
        let object: [String: Any] = ["version": 1, "blocked_count": 25, "events": events]
        try JSONSerialization.data(withJSONObject: object).write(to: file)

        let snapshot = ProtectionAuditReader(url: file).read()

        XCTAssertEqual(snapshot.blockedCount, ProtectionAuditSnapshot.maxRetainedEvents)
        XCTAssertEqual(snapshot.lastCategory, "category-24")
        XCTAssertEqual(snapshot.lastAt, "2026-09-13T00:00:24Z")
    }

    func testNativeBackendUsesExplicitLifecycleScriptActionsAndCanonicalStatus() throws {
        let root = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent("agent-runtime-backend-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let log = root.appendingPathComponent("actions.log")
        let running = root.appendingPathComponent("running")
        let script = root.appendingPathComponent("start.sh")
        try """
        #!/bin/bash
        set -e
        case "$1" in
          start|restart)
            echo "$1" >> "\(log.path)"
            touch "\(running.path)"
            ;;
          stop)
            echo "$1" >> "\(log.path)"
            rm -f "\(running.path)"
            ;;
          status)
            [[ "$2" == "--json" ]] || exit 9
            if [[ -f "\(running.path)" ]]; then
              printf '%s\n' '{"schema":1,"state":"running","control":"managed","pids":[501],"health":"live","ready":"ready","desired":"running","detail":"Canonical Runtime is serving and ready."}'
            else
              printf '%s\n' '{"schema":1,"state":"stopped","control":"none","pids":[],"health":"unverified","ready":"unverified","desired":"stopped","detail":"No Runtime listener is serving."}'
            fi
            ;;
          *) exit 9 ;;
        esac
        """.write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: script.path)

        let identity = ProcessIdentity(pid: 501, processGroupID: 501, startSeconds: 1, startMicroseconds: 1, executablePath: "/opt/homebrew/bin/tunnel-client")
        let backend = NativeRuntimeBackend(
            configuration: RuntimeConfiguration(checkoutRoot: root.path, transitionTimeout: 0.2),
            inspector: FixtureInspector(identity: identity),
            discovery: ThrowingDiscovery()
        )

        try backend.startOwned()
        XCTAssertEqual(try backend.observeStatus(), .owned(identity))
        try backend.restartOwned()
        XCTAssertEqual(try backend.observeStatus(), .owned(identity))
        try backend.stopOwned()
        XCTAssertEqual(try backend.observeStatus(), .stopped)
        XCTAssertEqual(try String(contentsOf: log).split(separator: "\n").map(String.init), ["start", "restart", "stop"])
    }

    func testNativeBackendConsumesCanonicalStatusObservationWithoutIndependentDiscovery() throws {
        let root = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent("agent-runtime-status-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let statusFile = root.appendingPathComponent("status.json")
        let script = root.appendingPathComponent("start.sh")
        try """
        #!/bin/bash
        set -e
        [[ "$1" == "status" && "$2" == "--json" ]] || exit 9
        cat "\(statusFile.path)"
        """.write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: script.path)

        let identity = ProcessIdentity(pid: 501, processGroupID: 501, startSeconds: 1, startMicroseconds: 1, executablePath: "/opt/homebrew/bin/tunnel-client")
        let backend = NativeRuntimeBackend(
            configuration: RuntimeConfiguration(checkoutRoot: root.path, transitionTimeout: 0.2),
            inspector: FixtureInspector(identity: identity),
            discovery: ThrowingDiscovery()
        )

        try #"{"schema":1,"state":"running","control":"managed","pids":[501],"health":"live","ready":"ready","desired":"running","detail":"Canonical Runtime is serving and ready."}"#.write(to: statusFile, atomically: true, encoding: .utf8)
        XCTAssertEqual(try backend.observeStatus(), .owned(identity))

        try #"{"schema":1,"state":"running","control":"read-only","pids":[501],"health":"live","ready":"ready","desired":"running","detail":"Canonical Runtime is serving and ready."}"#.write(to: statusFile, atomically: true, encoding: .utf8)
        XCTAssertEqual(try backend.observeStatus(), .external([501]))

        try #"{"schema":1,"state":"attention","control":"none","pids":[501],"health":"live","ready":"failed","desired":"running","detail":"Canonical Runtime identity is present, but readyz is not green."}"#.write(to: statusFile, atomically: true, encoding: .utf8)
        XCTAssertEqual(try backend.observeStatus(), .ambiguous("Canonical Runtime identity is present, but readyz is not green."))

        try #"{"schema":1,"state":"stopped","control":"none","pids":[],"health":"unverified","ready":"unverified","desired":"stopped","detail":"No Runtime listener is serving."}"#.write(to: statusFile, atomically: true, encoding: .utf8)
        XCTAssertEqual(try backend.observeStatus(), .stopped)
    }

    func testDesiredStoppedCanonicalServingObservationRemainsReadOnlyExternal() throws {
        let root = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent("agent-runtime-stale-desired-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let script = root.appendingPathComponent("start.sh")
        try """
        #!/bin/bash
        [[ "$1" == "status" && "$2" == "--json" ]] || exit 9
        printf '%s\n' '{"schema":1,"state":"running","control":"read-only","pids":[601],"health":"live","ready":"ready","desired":"stopped","detail":"Canonical Runtime is serving and ready."}'
        """.write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: script.path)

        let identity = ProcessIdentity(pid: 601, processGroupID: 601, startSeconds: 1, startMicroseconds: 1, executablePath: "/opt/homebrew/bin/tunnel-client")
        let backend = NativeRuntimeBackend(
            configuration: RuntimeConfiguration(checkoutRoot: root.path, transitionTimeout: 0),
            inspector: FixtureInspector(identity: identity),
            discovery: ThrowingDiscovery()
        )
        XCTAssertEqual(try backend.observeStatus(), .external([601]))
    }
}

private final class ThrowingDiscovery: RuntimeDiscovering {
    func matchingRuntimePIDs(checkoutRoot: String) throws -> [Int32] {
        throw RuntimeLifecycleError.operationFailed("independent process discovery must not define production status")
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
