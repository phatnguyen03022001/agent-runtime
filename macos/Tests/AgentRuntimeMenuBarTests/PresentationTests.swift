import AgentRuntimeCore
import Foundation
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
            sessionLimit: 64,
            parallelLimit: 4
        )
        XCTAssertEqual(
            presentation.facts.map(\.label),
            ["Endpoint", "PID", "Health", "Ready", "Sessions", "Parallel", "Protection"]
        )
        XCTAssertEqual(presentation.facts.map(\.value), ["127.0.0.1:8080", "42", "live", "ready", "64 max", "4 max", "Clear"])
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

    func testPopoverHeaderUsesCircleStatusDotWithTruthfulSemantics() {
        let online = RuntimePopoverStatusIndicator(status: .owned(servingIdentity))
        XCTAssertEqual(online.symbolName, "circle.fill")
        XCTAssertEqual(online.color, .systemGreen)
        XCTAssertTrue(online.accessibilityLabel.contains("Online"))

        for status in [RuntimeStatus.stopped, .external([99]), .ambiguous("health unavailable")] {
            let unavailable = RuntimePopoverStatusIndicator(status: status)
            XCTAssertEqual(unavailable.symbolName, "circle.fill")
            XCTAssertEqual(unavailable.color, .systemRed)
            XCTAssertTrue(unavailable.accessibilityLabel.contains("Offline or unconfirmed"))
        }
    }

    func testMenuBarStatusItemFollowsNativeAppearanceAndCommunicatesState() {
        let online = RuntimeStatusIndicator(status: .owned(servingIdentity))
        XCTAssertEqual(online, .online)
        XCTAssertEqual(online.symbolName, "bolt.horizontal.circle.fill")
        XCTAssertFalse(online.appearsDisabled)
        XCTAssertTrue(online.accessibilityLabel.contains("Serving"))

        for status in [RuntimeStatus.stopped, .external([99]), .ambiguous("health unavailable")] {
            let offline = RuntimeStatusIndicator(status: status)
            XCTAssertEqual(offline, .offlineOrUnconfirmed)
            XCTAssertEqual(offline.symbolName, "bolt.horizontal.circle")
            XCTAssertTrue(offline.appearsDisabled)
            XCTAssertTrue(offline.accessibilityLabel.contains("Not serving"))
        }

        // Verify template image configuration
        let config = NSImage.SymbolConfiguration(pointSize: 13, weight: .medium)
        let onlineImage = NSImage(systemSymbolName: online.symbolName, accessibilityDescription: online.accessibilityLabel)?
            .withSymbolConfiguration(config)
        XCTAssertNotNil(onlineImage)
        onlineImage?.isTemplate = true
        XCTAssertTrue(onlineImage?.isTemplate == true)

        let offlineImage = NSImage(systemSymbolName: RuntimeStatusIndicator(status: .stopped).symbolName, accessibilityDescription: nil)?
            .withSymbolConfiguration(config)
        XCTAssertNotNil(offlineImage)
        offlineImage?.isTemplate = true
        XCTAssertTrue(offlineImage?.isTemplate == true)
    }

    @MainActor
    func testStatusItemButtonRemainsClickableWithoutForcedTint() {
        let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        guard let button = statusItem.button else {
            return XCTFail("status item button must be available")
        }

        // Simulate online update
        let onlineStatus = RuntimeStatus.owned(servingIdentity)
        let onlineIndicator = RuntimeStatusIndicator(status: onlineStatus)
        button.image = NSImage(systemSymbolName: onlineIndicator.symbolName, accessibilityDescription: onlineIndicator.accessibilityLabel)
        button.image?.isTemplate = true
        button.contentTintColor = nil
        button.appearsDisabled = onlineIndicator.appearsDisabled
        let onlineSummary = RuntimePopoverPresentation.accessibilitySummary(for: onlineStatus)
        button.toolTip = onlineSummary
        button.setAccessibilityValue(onlineSummary)

        XCTAssertFalse(button.appearsDisabled)
        XCTAssertNil(button.contentTintColor)
        XCTAssertTrue(button.isEnabled)
        XCTAssertEqual(button.toolTip, onlineSummary)

        // Simulate offline update
        let stoppedStatus = RuntimeStatus.stopped
        let offlineIndicator = RuntimeStatusIndicator(status: stoppedStatus)
        button.image = NSImage(systemSymbolName: offlineIndicator.symbolName, accessibilityDescription: offlineIndicator.accessibilityLabel)
        button.image?.isTemplate = true
        button.contentTintColor = nil
        button.appearsDisabled = offlineIndicator.appearsDisabled
        let stoppedSummary = RuntimePopoverPresentation.accessibilitySummary(for: stoppedStatus)
        button.toolTip = stoppedSummary
        button.setAccessibilityValue(stoppedSummary)

        XCTAssertTrue(button.appearsDisabled)
        XCTAssertNil(button.contentTintColor)
        XCTAssertTrue(button.isEnabled, "Button must remain clickable when Runtime is offline")
        XCTAssertEqual(button.toolTip, stoppedSummary)
    }

    @MainActor
    func testLiquidGlassAvailabilityAndFallbackContracts() {
        let content = NSView(frame: NSRect(x: 0, y: 0, width: 304, height: 100))
        let background = LiquidGlassSupport.makeBackgroundView(embedding: content)

        if #available(macOS 26.0, *) {
            XCTAssertTrue(LiquidGlassSupport.isLiquidGlassSupported)
            guard let glass = background as? NSGlassEffectView else {
                return XCTFail("background must be an NSGlassEffectView on macOS 26+")
            }
            XCTAssertEqual(glass.style, .regular)
            XCTAssertNil(glass.tintColor)
            XCTAssertEqual(glass.cornerRadius, 10.0)
            XCTAssertTrue(glass.contentView === content)
            if #available(macOS 27.0, *) {
                XCTAssertTrue(glass.effectIsInteractive)
            }
        } else {
            XCTAssertFalse(LiquidGlassSupport.isLiquidGlassSupported)
            XCTAssertTrue(background.subviews.contains(content))
        }
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

    func testFirstRunLaunchPolicyShowsFreshSetupAndBypassesValidInstalledConfiguration() {
        let workspace = URL(fileURLWithPath: "/workspace", isDirectory: true)

        XCTAssertEqual(
            FirstRunLaunchPolicy.decide(configuration: .missing, activationRequired: false),
            .showFresh
        )
        XCTAssertEqual(
            FirstRunLaunchPolicy.decide(configuration: .valid(workspace: workspace), activationRequired: false),
            .controlPanel
        )
        XCTAssertEqual(
            FirstRunLaunchPolicy.decide(configuration: .valid(workspace: workspace), activationRequired: true),
            .showConfigured(workspace)
        )
        XCTAssertEqual(
            FirstRunLaunchPolicy.decide(configuration: .invalid("unsafe"), activationRequired: false),
            .showBlocked("unsafe")
        )
    }

    func testFirstRunDoctorGateAllowsOnlyRecognizedPendingTransactionWarning() throws {
        let allowed = try doctorReport(
            status: "degraded",
            checks: [
                check("runtime_identity", "pass", "OK"),
                check(
                    "cutover_identity",
                    "warn",
                    "CUTOVER_TRANSACTION_PRESENT",
                    evidence: ["transaction_present": true, "status": "PENDING", "phase": "APP_SWAPPED"]
                ),
            ]
        )
        XCTAssertEqual(FirstRunDoctorGate.preCommitDecision(from: allowed), .commitAllowed)

        let extraWarning = try doctorReport(
            status: "degraded",
            checks: [
                check("service_registration", "warn", "SERVICE_NOT_REGISTERED"),
                check(
                    "cutover_identity",
                    "warn",
                    "CUTOVER_TRANSACTION_PRESENT",
                    evidence: ["transaction_present": true, "status": "PENDING", "phase": "APP_SWAPPED"]
                ),
            ]
        )
        XCTAssertEqual(FirstRunDoctorGate.preCommitDecision(from: extraWarning), .blocked)

        let failure = try doctorReport(
            status: "unhealthy",
            checks: [
                check("workspace", "fail", "WORKSPACE_UNAVAILABLE"),
                check(
                    "cutover_identity",
                    "warn",
                    "CUTOVER_TRANSACTION_PRESENT",
                    evidence: ["transaction_present": true, "status": "PENDING", "phase": "APP_SWAPPED"]
                ),
            ]
        )
        XCTAssertEqual(FirstRunDoctorGate.preCommitDecision(from: failure), .blocked)

        let malformed = try doctorReport(
            status: "degraded",
            checks: [
                check("runtime_identity", "unknown", "UNEXPECTED"),
                check(
                    "cutover_identity",
                    "warn",
                    "CUTOVER_TRANSACTION_PRESENT",
                    evidence: ["transaction_present": true, "status": "PENDING", "phase": "APP_SWAPPED"]
                ),
            ]
        )
        XCTAssertEqual(FirstRunDoctorGate.preCommitDecision(from: malformed), .blocked)
    }

    func testFirstRunDoctorGateRequiresHealthyTransactionFreePostCommitState() throws {
        let healthy = try doctorReport(
            status: "healthy",
            checks: [
                check("runtime_identity", "pass", "OK"),
                check(
                    "cutover_identity",
                    "pass",
                    "OK",
                    evidence: ["transaction_present": false]
                ),
            ]
        )
        XCTAssertTrue(FirstRunDoctorGate.postCommitIsHealthy(from: healthy))

        let pending = try doctorReport(
            status: "degraded",
            checks: [
                check(
                    "cutover_identity",
                    "warn",
                    "CUTOVER_TRANSACTION_PRESENT",
                    evidence: ["transaction_present": true, "status": "PENDING", "phase": "APP_SWAPPED"]
                ),
            ]
        )
        XCTAssertFalse(FirstRunDoctorGate.postCommitIsHealthy(from: pending))

        let malformedHealthy = try doctorReport(
            status: "healthy",
            checks: [
                check("runtime_identity", "unknown", "UNEXPECTED"),
                check("cutover_identity", "pass", "OK", evidence: ["transaction_present": false]),
            ]
        )
        XCTAssertFalse(FirstRunDoctorGate.postCommitIsHealthy(from: malformedHealthy))
    }

    func testFirstRunDoctorGateSurfacesApprovalAndPartialRecoveryWithoutCommit() throws {
        let approval = try doctorReport(
            status: "degraded",
            checks: [
                check("service_registration", "warn", "SERVICE_APPROVAL_REQUIRED"),
                check(
                    "cutover_identity",
                    "warn",
                    "CUTOVER_TRANSACTION_PRESENT",
                    evidence: ["transaction_present": true, "status": "AWAITING_APPROVAL", "phase": "APP_SWAPPED"]
                ),
            ]
        )
        XCTAssertEqual(FirstRunDoctorGate.preCommitDecision(from: approval), .approvalRequired)

        let partial = try doctorReport(
            status: "degraded",
            checks: [
                check(
                    "cutover_identity",
                    "warn",
                    "CUTOVER_TRANSACTION_PRESENT",
                    evidence: ["transaction_present": true, "status": "PARTIAL", "phase": "APP_SWAPPED"]
                ),
            ]
        )
        XCTAssertEqual(FirstRunDoctorGate.preCommitDecision(from: partial), .recoverPartial)
    }

    func testFirstRunExistingCanonicalInspectionPreservesBytesAndBypassesInstalledSetup() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        let home = root.appendingPathComponent("home", isDirectory: true)
        let candidate = home.appendingPathComponent("Applications/Agent Runtime.app", isDirectory: true)
        let workspace = root.appendingPathComponent("workspace", isDirectory: true)
        let canonical = home.appendingPathComponent(
            "Library/Application Support/Agent Runtime/runtime.env",
            isDirectory: false
        )
        try FileManager.default.createDirectory(at: candidate, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: workspace, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: canonical.deletingLastPathComponent(), withIntermediateDirectories: true)
        let before = Data("opaque-canonical-bytes".utf8)
        try before.write(to: canonical)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }

        let inspection = try JSONSerialization.data(
            withJSONObject: [
                "git_identity_ready": true,
                "workspace_root": workspace.path,
            ],
            options: [.sortedKeys]
        )
        let runner = RecordingFirstRunRunner(results: [
            .init(exitCode: 0, standardOutput: inspection),
        ])
        let orchestrator = FirstRunSetupOrchestrator(
            paths: FirstRunSetupPaths(candidateApp: candidate, home: home),
            runner: runner
        )

        XCTAssertEqual(orchestrator.inspectConfiguration(), .valid(workspace: workspace))
        XCTAssertFalse(orchestrator.requiresOnboardingForValidConfiguration())
        XCTAssertEqual(try Data(contentsOf: canonical), before)
        XCTAssertEqual(runner.invocations.count, 1)
        XCTAssertNil(runner.invocations[0].standardInput)
    }

    func testFirstRunExtraDoctorWarningBlocksCommit() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        let home = root.appendingPathComponent("home", isDirectory: true)
        let candidate = root.appendingPathComponent("release/Agent Runtime.app", isDirectory: true)
        let workspace = root.appendingPathComponent("workspace", isDirectory: true)
        let transaction = home.appendingPathComponent(
            "Library/Application Support/Agent Runtime/cutover-transaction",
            isDirectory: true
        )
        try FileManager.default.createDirectory(at: candidate, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: workspace, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: transaction, withIntermediateDirectories: true)
        try Data("{}".utf8).write(
            to: candidate.deletingLastPathComponent().appendingPathComponent("Agent Runtime.candidate.json")
        )
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }

        let blockedDoctor = try doctorReport(
            status: "degraded",
            checks: [
                check("service_registration", "warn", "SERVICE_NOT_REGISTERED"),
                check(
                    "cutover_identity",
                    "warn",
                    "CUTOVER_TRANSACTION_PRESENT",
                    evidence: ["transaction_present": true, "status": "PENDING", "phase": "APP_SWAPPED"]
                ),
            ]
        )
        let runner = RecordingFirstRunRunner(results: [
            .init(exitCode: 0),
            .init(exitCode: 0),
            .init(exitCode: 1, standardOutput: blockedDoctor),
        ])
        let orchestrator = FirstRunSetupOrchestrator(
            paths: FirstRunSetupPaths(candidateApp: candidate, home: home),
            runner: runner
        )

        let outcome = orchestrator.begin(
            FirstRunSetupInput(
                apiKey: "API_SENTINEL",
                tunnelID: "TUNNEL_SENTINEL",
                workspace: workspace,
                gitName: "Native Operator",
                gitEmail: "native@example.invalid"
            )
        )

        XCTAssertEqual(
            outcome,
            .actionRequired("Setup has not reached the safe commit gate. No commit was attempted.", .rollback)
        )
        XCTAssertFalse(
            runner.invocations.contains(where: { $0.arguments.contains("--commit-cutover") })
        )
    }

    func testFirstRunRejectsInvalidWorkspaceBeforeConfigurationOrLifecycleDispatch() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        let home = root.appendingPathComponent("home", isDirectory: true)
        let candidate = root.appendingPathComponent("release/Agent Runtime.app", isDirectory: true)
        let missingWorkspace = root.appendingPathComponent("missing-workspace", isDirectory: true)
        try FileManager.default.createDirectory(at: candidate, withIntermediateDirectories: true)
        try Data("{}".utf8).write(
            to: candidate.deletingLastPathComponent().appendingPathComponent("Agent Runtime.candidate.json")
        )
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }

        let runner = RecordingFirstRunRunner(results: [])
        let paths = FirstRunSetupPaths(candidateApp: candidate, home: home)
        let orchestrator = FirstRunSetupOrchestrator(paths: paths, runner: runner)

        let outcome = orchestrator.begin(
            FirstRunSetupInput(
                apiKey: "not-dispatched",
                tunnelID: "not-dispatched",
                workspace: missingWorkspace,
                gitName: "Native Operator",
                gitEmail: "native@example.invalid"
            )
        )

        XCTAssertEqual(outcome, .failure("Choose an existing workspace folder."))
        XCTAssertTrue(runner.invocations.isEmpty)
        XCTAssertFalse(FileManager.default.fileExists(atPath: paths.canonicalEnv.path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: paths.transactionDirectory.path))
    }

    func testFirstRunOrchestratorKeepsSecretsOffArgvAndUsesExistingLifecycle() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        let home = root.appendingPathComponent("home", isDirectory: true)
        let candidate = root.appendingPathComponent("release/Agent Runtime.app", isDirectory: true)
        let workspace = root.appendingPathComponent("workspace", isDirectory: true)
        try FileManager.default.createDirectory(at: candidate, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: workspace, withIntermediateDirectories: true)
        try Data("{}".utf8).write(to: candidate.deletingLastPathComponent().appendingPathComponent("Agent Runtime.candidate.json"))
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }

        let preCommit = try doctorReport(
            status: "degraded",
            checks: [
                check("runtime_identity", "pass", "OK"),
                check(
                    "cutover_identity",
                    "warn",
                    "CUTOVER_TRANSACTION_PRESENT",
                    evidence: ["transaction_present": true, "status": "PENDING", "phase": "APP_SWAPPED"]
                ),
            ]
        )
        let postCommit = try doctorReport(
            status: "healthy",
            checks: [
                check("runtime_identity", "pass", "OK"),
                check("cutover_identity", "pass", "OK", evidence: ["transaction_present": false]),
            ]
        )
        let runner = RecordingFirstRunRunner(results: [
            .init(exitCode: 0),
            .init(exitCode: 0),
            .init(exitCode: 1, standardOutput: preCommit),
            .init(exitCode: 0),
            .init(exitCode: 0),
            .init(exitCode: 0, standardOutput: postCommit),
            .init(exitCode: 0),
        ])
        let paths = FirstRunSetupPaths(candidateApp: candidate, home: home)
        let orchestrator = FirstRunSetupOrchestrator(paths: paths, runner: runner)
        let api = "SWIFT_SENTINEL_" + UUID().uuidString
        let tunnel = "SWIFT_SENTINEL_" + UUID().uuidString

        let outcome = orchestrator.begin(
            FirstRunSetupInput(
                apiKey: api,
                tunnelID: tunnel,
                workspace: workspace,
                gitName: "Native Operator",
                gitEmail: "native@example.invalid"
            )
        )

        XCTAssertEqual(outcome, .success)
        XCTAssertEqual(runner.invocations.count, 7)
        XCTAssertEqual(runner.invocations[0].arguments.prefix(2), [paths.runtimeConfig.path, "--prebuilt-stdin"])
        XCTAssertEqual(runner.invocations[1].arguments, [paths.installer.path, "--workspace-root", workspace.path])
        XCTAssertEqual(runner.invocations[4].arguments, [paths.installer.path, "--commit-cutover"])
        for invocation in runner.invocations {
            XCTAssertFalse(invocation.arguments.joined(separator: " ").contains(api))
            XCTAssertFalse(invocation.arguments.joined(separator: " ").contains(tunnel))
        }
        let firstInput = try XCTUnwrap(runner.invocations.first?.standardInput)
        XCTAssertTrue(String(decoding: firstInput, as: UTF8.self).contains(api))
        XCTAssertTrue(String(decoding: firstInput, as: UTF8.self).contains(tunnel))
        XCTAssertNil(runner.invocations[1].standardInput)
    }

    @MainActor
    func testFirstRunWorkspacePickerIsDirectoryOnlyAndCancelDoesNotChangeSelection() {
        let controller = FirstRunSetupController(
            mode: .fresh,
            begin: { _ in .failure("not called") },
            activateConfigured: { _ in .failure("not called") },
            performAction: { _ in .failure("not called") },
            completed: {}
        )
        controller.loadView()
        let apiType = type(of: controller.apiKeyField)
        let tunnelType = type(of: controller.tunnelIDField)
        let workspaceEditable = controller.workspaceField.isEditable
        XCTAssertTrue(apiType == NSSecureTextField.self)
        XCTAssertTrue(tunnelType == NSSecureTextField.self)
        XCTAssertFalse(workspaceEditable)

        let panel = NSOpenPanel()
        FirstRunSetupController.configureWorkspacePanel(panel)
        let canChooseDirectories = panel.canChooseDirectories
        let canChooseFiles = panel.canChooseFiles
        let allowsMultipleSelection = panel.allowsMultipleSelection
        XCTAssertTrue(canChooseDirectories)
        XCTAssertFalse(canChooseFiles)
        XCTAssertFalse(allowsMultipleSelection)

        let original = URL(fileURLWithPath: "/existing")
        controller.selectedWorkspace = original
        controller.applyWorkspaceSelection(response: .cancel, urls: [URL(fileURLWithPath: "/ignored")])
        let selectedWorkspace = controller.selectedWorkspace
        XCTAssertEqual(selectedWorkspace, original)
    }

    private func check(
        _ id: String,
        _ status: String,
        _ reason: String,
        evidence: [String: Any]? = nil
    ) -> [String: Any] {
        var value: [String: Any] = [
            "id": id,
            "status": status,
            "reason_code": reason,
            "message": "fixture",
        ]
        if let evidence { value["evidence"] = evidence }
        return value
    }

    private func doctorReport(status: String, checks: [[String: Any]]) throws -> Data {
        try JSONSerialization.data(
            withJSONObject: [
                "schema_version": 1,
                "runtime_version": "0.5.0",
                "status": status,
                "checks": checks,
            ],
            options: [.sortedKeys]
        )
    }

}


private final class RecordingFirstRunRunner: FirstRunProcessRunning {
    struct Invocation {
        let executable: URL
        let arguments: [String]
        let standardInput: Data?
    }

    private var results: [FirstRunProcessResult]
    private(set) var invocations: [Invocation] = []

    init(results: [FirstRunProcessResult]) {
        self.results = results
    }

    func run(executable: URL, arguments: [String], standardInput: Data?) -> FirstRunProcessResult {
        invocations.append(Invocation(executable: executable, arguments: arguments, standardInput: standardInput))
        return results.isEmpty ? FirstRunProcessResult(exitCode: 99) : results.removeFirst()
    }
}
