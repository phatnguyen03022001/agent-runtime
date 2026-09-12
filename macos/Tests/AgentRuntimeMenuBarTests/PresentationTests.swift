import AgentRuntimeCore
@testable import AgentRuntimeMenuBar
import XCTest

final class PresentationTests: XCTestCase {
    private let servingIdentity = ProcessIdentity(
        pid: 42,
        processGroupID: 42,
        startSeconds: 1,
        startMicroseconds: 2,
        executablePath: "/usr/bin/tunnel-client"
    )

    func testOfflineAudioPlaysOncePerConnectedToOfflineEdge() {
        var policy = OfflineAudioPolicy()

        XCTAssertFalse(policy.shouldPlay(for: .stopped))
        XCTAssertFalse(policy.shouldPlay(for: .owned(servingIdentity)))
        XCTAssertFalse(policy.shouldPlay(for: .owned(servingIdentity)))
        XCTAssertTrue(policy.shouldPlay(for: .stopped))
        XCTAssertFalse(policy.shouldPlay(for: .stopped))
        XCTAssertFalse(policy.shouldPlay(for: .owned(servingIdentity)))
        XCTAssertTrue(policy.shouldPlay(for: .ambiguous("health unavailable")))
        XCTAssertFalse(policy.shouldPlay(for: .ambiguous("still unavailable")))
    }

    func testOfflineAudioIgnoresExternalAndFirstAttentionObservations() {
        var policy = OfflineAudioPolicy()

        XCTAssertFalse(policy.shouldPlay(for: .ambiguous("first observation")))
        XCTAssertFalse(policy.shouldPlay(for: .external([99])))
        XCTAssertFalse(policy.shouldPlay(for: .stopped))
        XCTAssertFalse(policy.shouldPlay(for: .external([99])))
        XCTAssertFalse(policy.shouldPlay(for: .owned(servingIdentity)))
        XCTAssertFalse(policy.shouldPlay(for: .external([99])))
        XCTAssertFalse(policy.shouldPlay(for: .external([99])))
        XCTAssertTrue(policy.shouldPlay(for: .stopped))
    }

    func testFactsUseStableTwoColumnAlignmentContract() {
        XCTAssertEqual(RuntimeFactLayout.labelColumn, 0)
        XCTAssertEqual(RuntimeFactLayout.valueColumn, 1)
        XCTAssertEqual(RuntimeFactLayout.rowSpacing, 5)
        XCTAssertEqual(RuntimeFactLayout.columnSpacing, 12)
        XCTAssertEqual(RuntimeFactLayout.valueAlignment, .right)

        let presentation = RuntimePopoverPresentation.make(
            status: .owned(servingIdentity),
            audit: ProtectionAuditSnapshot(),
            sessionLimit: 64
        )
        XCTAssertEqual(
            presentation.facts.map(\.label),
            ["Endpoint", "PID", "Health", "Ready", "Sessions", "Protection"]
        )
        XCTAssertEqual(presentation.facts.map(\.value), ["127.0.0.1:8080", "42", "live", "ready", "64 max", "Clear"])
    }

    func testLifecyclePresentationExposesExactlyOneStateAction() {
        let connected = RuntimePopoverPresentation.make(
            status: .owned(servingIdentity),
            audit: ProtectionAuditSnapshot(),
            sessionLimit: 64
        )
        if case .stop? = connected.lifecycleAction {} else { XCTFail("connected Runtime should expose only Stop") }

        let stopped = RuntimePopoverPresentation.make(
            status: .stopped,
            audit: ProtectionAuditSnapshot(),
            sessionLimit: 64
        )
        if case .start? = stopped.lifecycleAction {} else { XCTFail("stopped Runtime should expose only Start") }

        for status in [RuntimeStatus.external([99]), .ambiguous("unavailable")] {
            let unavailable = RuntimePopoverPresentation.make(
                status: status,
                audit: ProtectionAuditSnapshot(),
                sessionLimit: 64
            )
            XCTAssertNil(unavailable.lifecycleAction)
        }
    }

    func testStatusIndicatorUsesGreenOnlyForServingReadyRuntime() {
        XCTAssertEqual(RuntimeStatusIndicator(status: .owned(servingIdentity)), .green)
        XCTAssertEqual(RuntimeStatusIndicator(status: .stopped), .red)
        XCTAssertEqual(RuntimeStatusIndicator(status: .external([99])), .red)
        XCTAssertEqual(RuntimeStatusIndicator(status: .ambiguous("health unavailable")), .red)
    }

    func testAccessibilitySummaryCarriesStateWithoutDependingOnColor() {
        let connected = RuntimePopoverPresentation.accessibilitySummary(for: .owned(servingIdentity))
        XCTAssertTrue(connected.contains("Connected"))
        XCTAssertTrue(connected.contains("live and ready"))

        let stopped = RuntimePopoverPresentation.accessibilitySummary(for: .stopped)
        XCTAssertTrue(stopped.contains("Offline"))
        XCTAssertTrue(stopped.contains("STOPPED"))

        let attention = RuntimePopoverPresentation.accessibilitySummary(for: .ambiguous("health unavailable"))
        XCTAssertTrue(attention.contains("Attention"))
        XCTAssertTrue(attention.contains("health unavailable"))
    }

    func testProtectionPresentationUsesRetainedBoundedHistoryWithoutRawCategory() {
        let presentation = RuntimePopoverPresentation.make(
            status: .owned(servingIdentity),
            audit: ProtectionAuditSnapshot(
                blockedCount: 25,
                lastCategory: "canonical_process_signal",
                lastAt: "2026-09-13T00:00:00Z"
            ),
            sessionLimit: 64
        )
        XCTAssertEqual(presentation.facts.last?.value, "20 retained")
        XCTAssertFalse(presentation.facts.last?.value.contains("canonical_process_signal") == true)
    }
}
