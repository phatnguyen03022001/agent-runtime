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

    func testLifecyclePresentationKeepsOneStablePolicyControlledActionSlot() {
        let connected = RuntimePopoverPresentation.make(
            status: .owned(servingIdentity),
            audit: ProtectionAuditSnapshot(),
            sessionLimit: 64
        )
        if case .stop = connected.lifecycleSlot.action {} else { XCTFail("connected Runtime should expose Stop") }
        XCTAssertTrue(connected.lifecycleSlot.isEnabled)

        let stopped = RuntimePopoverPresentation.make(
            status: .stopped,
            audit: ProtectionAuditSnapshot(),
            sessionLimit: 64
        )
        if case .start = stopped.lifecycleSlot.action {} else { XCTFail("stopped Runtime should expose Start") }
        XCTAssertTrue(stopped.lifecycleSlot.isEnabled)

        for status in [RuntimeStatus.external([99]), .ambiguous("unavailable")] {
            let unavailable = RuntimePopoverPresentation.make(
                status: status,
                audit: ProtectionAuditSnapshot(),
                sessionLimit: 64
            )
            if case .stop = unavailable.lifecycleSlot.action {} else { XCTFail("unavailable Runtime should retain a Stop slot") }
            XCTAssertFalse(unavailable.lifecycleSlot.isEnabled)
        }
    }

    func testMenuBarGlyphIsPreservedWhileTintMapsServingState() {
        let indicators = [
            RuntimeStatusIndicator(status: .owned(servingIdentity)),
            RuntimeStatusIndicator(status: .stopped),
            RuntimeStatusIndicator(status: .external([99])),
            RuntimeStatusIndicator(status: .ambiguous("health unavailable")),
        ]

        XCTAssertEqual(indicators.map(\.symbolName), Array(repeating: "bolt.horizontal.circle.fill", count: indicators.count))
        XCTAssertEqual(indicators[0], .green)
        XCTAssertEqual(indicators[1], .red)
        XCTAssertEqual(indicators[2], .red)
        XCTAssertEqual(indicators[3], .red)
    }

    @MainActor
    func testHeaderSpansContentWidthWithTrailingStatusIndicator() {
        let controller = ControlPanelController(performAction: { _ in }, quit: {})
        controller.loadView()

        guard let rootStack = controller.header.superview as? NSStackView else {
            return XCTFail("header should remain in the content stack")
        }
        XCTAssertEqual(controller.header.arrangedSubviews.count, 2)
        XCTAssertTrue(controller.header.arrangedSubviews.last is NSImageView)
        XCTAssertTrue(rootStack.constraints.contains { constraint in
            constraint.isActive
                && constraint.firstItem === controller.header
                && constraint.secondItem === rootStack
                && constraint.firstAttribute == .width
                && constraint.secondAttribute == .width
        })
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
