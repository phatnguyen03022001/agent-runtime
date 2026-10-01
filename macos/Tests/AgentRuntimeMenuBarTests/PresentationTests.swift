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

    @MainActor
    func testInstalledConfigurationAcceptsCurrentSchemaZeroCostSubstrateWithoutEmbeddedServer() throws {
        let fixture = try installedConfigurationFixture()
        XCTAssertFalse(FileManager.default.fileExists(
            atPath: fixture.resources.appendingPathComponent("runtime/agent_runtime/server.py").path
        ))

        let configuration = try AppDelegate.installedRuntimeConfiguration(
            resources: fixture.resources,
            home: fixture.home
        )
        XCTAssertEqual(configuration.runtimeRoot, fixture.resources.appendingPathComponent("runtime").path)
        XCTAssertEqual(configuration.envFileURL, fixture.env)
        XCTAssertTrue(configuration.requiresReadiness)
        XCTAssertEqual(configuration.sessionLimit, 6)
        XCTAssertEqual(configuration.parallelLimit, 2)
    }

    @MainActor
    func testInstalledConfigurationRejectsLegacyAndUnsupportedManifestSchemas() throws {
        for schema in [1, 4] {
            let fixture = try installedConfigurationFixture()
            let manifestURL = fixture.resources.appendingPathComponent("runtime-manifest.json")
            var manifest = try XCTUnwrap(
                JSONSerialization.jsonObject(with: Data(contentsOf: manifestURL)) as? [String: Any]
            )
            manifest["schema"] = schema
            try JSONSerialization.data(withJSONObject: manifest).write(to: manifestURL)
            XCTAssertThrowsError(try AppDelegate.installedRuntimeConfiguration(
                resources: fixture.resources,
                home: fixture.home
            ), "schema=\(schema)") { error in
                XCTAssertEqual(error as? RuntimeLifecycleError, .metadata("installed Runtime manifest is invalid"))
            }
        }
    }

    @MainActor
    func testInstalledConfigurationRejectsMissingOrNonExecutableSubstrateEntrypoints() throws {
        for relativePath in ["runtime/start.sh", "runtime/.venv/bin/python"] {
            for missing in [true, false] {
                let fixture = try installedConfigurationFixture()
                let entrypoint = fixture.resources.appendingPathComponent(relativePath)
                if missing {
                    try FileManager.default.removeItem(at: entrypoint)
                } else {
                    try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: entrypoint.path)
                }
                XCTAssertThrowsError(try AppDelegate.installedRuntimeConfiguration(
                    resources: fixture.resources,
                    home: fixture.home
                ), "\(relativePath), missing=\(missing)")
            }
        }
    }

    @MainActor
    func testInstalledConfigurationRejectsMissingRegularFileOrSymlinkRuntimeRoot() throws {
        for shape in ["missing", "regular-file", "symlink"] {
            let fixture = try installedConfigurationFixture()
            let runtime = fixture.resources.appendingPathComponent("runtime")
            let movedRuntime = fixture.resources.appendingPathComponent("moved-runtime")
            try FileManager.default.moveItem(at: runtime, to: movedRuntime)
            if shape == "regular-file" {
                try Data("not a directory".utf8).write(to: runtime)
            } else if shape == "symlink" {
                try FileManager.default.createSymbolicLink(at: runtime, withDestinationURL: movedRuntime)
            }
            XCTAssertThrowsError(try AppDelegate.installedRuntimeConfiguration(
                resources: fixture.resources,
                home: fixture.home
            ), shape)
        }
    }

    @MainActor
    func testInstalledConfigurationRejectsMissingMalformedOrInvalidManifest() throws {
        let invalidFields: [(String, Any)] = [
            ("schema", 2),
            ("owner", "untrusted.owner"),
            ("entrypoint", "other/start.sh"),
            ("python", "other/python"),
            ("runtime_revision", "short"),
        ]
        for (field, value) in invalidFields {
            let fixture = try installedConfigurationFixture()
            let manifestURL = fixture.resources.appendingPathComponent("runtime-manifest.json")
            var manifest = try XCTUnwrap(
                JSONSerialization.jsonObject(with: Data(contentsOf: manifestURL)) as? [String: Any]
            )
            manifest[field] = value
            try JSONSerialization.data(withJSONObject: manifest).write(to: manifestURL)
            XCTAssertThrowsError(try AppDelegate.installedRuntimeConfiguration(
                resources: fixture.resources,
                home: fixture.home
            ), field)
        }
        for malformed in [false, true] {
            let fixture = try installedConfigurationFixture()
            let manifestURL = fixture.resources.appendingPathComponent("runtime-manifest.json")
            if malformed {
                try Data("not JSON".utf8).write(to: manifestURL)
            } else {
                try FileManager.default.removeItem(at: manifestURL)
            }
            XCTAssertThrowsError(try AppDelegate.installedRuntimeConfiguration(
                resources: fixture.resources,
                home: fixture.home
            ))
        }
    }

    @MainActor
    func testInstalledConfigurationRejectsMissingDirectorySymlinkOrUnsafeModeEnvironment() throws {
        for shape in ["missing", "directory", "symlink", "mode-0644", "mode-0400"] {
            let fixture = try installedConfigurationFixture()
            if shape.hasPrefix("mode-") {
                let mode = shape == "mode-0644" ? 0o644 : 0o400
                try FileManager.default.setAttributes([.posixPermissions: mode], ofItemAtPath: fixture.env.path)
            } else {
                let movedEnv = fixture.env.deletingLastPathComponent().appendingPathComponent("moved.env")
                try FileManager.default.moveItem(at: fixture.env, to: movedEnv)
                if shape == "directory" {
                    try FileManager.default.createDirectory(at: fixture.env, withIntermediateDirectories: false)
                } else if shape == "symlink" {
                    try FileManager.default.createSymbolicLink(at: fixture.env, withDestinationURL: movedEnv)
                }
            }
            XCTAssertThrowsError(try AppDelegate.installedRuntimeConfiguration(
                resources: fixture.resources,
                home: fixture.home
            ), shape)
        }
    }

    func testCanonicalManagedRunningObservationPresentsLiveReadyEndpoint() {
        // NativeRuntimeBackend maps validated schema-3 running/managed/live/ready
        // observations with one serving PID to owned status.
        let presentation = RuntimePopoverPresentation.make(
            observation: RuntimeObservation(status: .owned(servingIdentity), tunnelTransport: .healthy),
            audit: ProtectionAuditSnapshot(),
            sessionLimit: 6
        )
        XCTAssertEqual(presentation.facts[0], RuntimeFact(label: "Endpoint", value: "127.0.0.1:8080"))
        XCTAssertNotEqual(presentation.facts[0].value, "Unavailable")
        XCTAssertEqual(presentation.facts[2], RuntimeFact(label: "Health", value: "live"))
        XCTAssertEqual(presentation.facts[3], RuntimeFact(label: "Ready", value: "ready"))
        XCTAssertTrue(presentation.lifecycleSlot.isEnabled)
    }

    private func installedConfigurationFixture() throws -> (resources: URL, home: URL, env: URL) {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        let resources = root.appendingPathComponent("Agent Runtime.app/Contents/Resources", isDirectory: true)
        let home = root.appendingPathComponent("home", isDirectory: true)
        let env = home.appendingPathComponent("Library/Application Support/Agent Runtime/runtime.env")
        for relativePath in ["runtime/start.sh", "runtime/.venv/bin/python"] {
            let entrypoint = resources.appendingPathComponent(relativePath)
            try FileManager.default.createDirectory(at: entrypoint.deletingLastPathComponent(), withIntermediateDirectories: true)
            try Data("#!/bin/sh\nexit 0\n".utf8).write(to: entrypoint)
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: entrypoint.path)
        }
        let manifest: [String: Any] = [
            "schema": 3,
            "owner": "com.picmao.agent-runtime",
            "entrypoint": "runtime/start.sh",
            "python": "runtime/.venv/bin/python",
            "runtime_revision": String(repeating: "a", count: 40),
        ]
        try JSONSerialization.data(withJSONObject: manifest)
            .write(to: resources.appendingPathComponent("runtime-manifest.json"))
        try FileManager.default.createDirectory(at: env.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("AGENT_RUNTIME_MAX_ACTIVE_SESSIONS=6\nAGENT_RUNTIME_MAX_PARALLELISM=2\n".utf8).write(to: env)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: env.path)
        return (resources, home, env)
    }

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

    func testStatusRefreshCoalescerBoundsManyRequestsToOneActiveAndOneFollowUp() {
        var coalescer = StatusRefreshCoalescer()

        XCTAssertTrue(coalescer.request())
        for _ in 0..<100 {
            XCTAssertFalse(coalescer.request())
        }
        XCTAssertTrue(coalescer.isInFlight)
        XCTAssertTrue(coalescer.hasPendingFollowUp)

        XCTAssertTrue(coalescer.complete())
        XCTAssertFalse(coalescer.isInFlight)
        XCTAssertFalse(coalescer.hasPendingFollowUp)

        XCTAssertTrue(coalescer.request())
        XCTAssertFalse(coalescer.complete())
        XCTAssertFalse(coalescer.isInFlight)
        XCTAssertFalse(coalescer.hasPendingFollowUp)
    }

    func testFactsUseStableTwoColumnAlignmentContract() {
        XCTAssertEqual(RuntimeFactLayout.labelColumn, 0)
        XCTAssertEqual(RuntimeFactLayout.valueColumn, 1)
        XCTAssertEqual(RuntimeFactLayout.rowSpacing, 5)
        XCTAssertEqual(RuntimeFactLayout.columnSpacing, 12)
        XCTAssertEqual(RuntimeFactLayout.valueAlignment, .right)

        let presentation = RuntimePopoverPresentation.make(
            observation: RuntimeObservation(status: .owned(servingIdentity), tunnelTransport: .healthy),
            audit: ProtectionAuditSnapshot(),
            sessionLimit: 64,
            parallelLimit: 4
        )
        XCTAssertEqual(
            presentation.facts.map(\.label),
            ["Endpoint", "PID", "Health", "Ready", "Tunnel", "Sessions", "Parallel", "Protection"]
        )
        XCTAssertEqual(
            presentation.facts.map(\.value),
            ["127.0.0.1:8080", "42", "live", "ready", "Healthy", "64 max", "4 max", "Clear"]
        )
    }

    func testLifecyclePresentationKeepsOneStablePolicyControlledActionSlot() {
        let connected = RuntimePopoverPresentation.make(
            observation: RuntimeObservation(status: .owned(servingIdentity), tunnelTransport: .healthy),
            audit: ProtectionAuditSnapshot(),
            sessionLimit: 64
        )
        if case .stop = connected.lifecycleSlot.action {} else { XCTFail("connected Runtime should expose Stop") }
        XCTAssertTrue(connected.lifecycleSlot.isEnabled)

        let stopped = RuntimePopoverPresentation.make(
            observation: RuntimeObservation(status: .stopped, tunnelTransport: .notRunning),
            audit: ProtectionAuditSnapshot(),
            sessionLimit: 64
        )
        if case .start = stopped.lifecycleSlot.action {} else { XCTFail("stopped Runtime should expose Start") }
        XCTAssertTrue(stopped.lifecycleSlot.isEnabled)

        for observation in [
            RuntimeObservation(status: .external([99]), tunnelTransport: .healthy),
            RuntimeObservation(status: .ambiguous("unavailable"), tunnelTransport: .unconfirmed),
        ] {
            let unavailable = RuntimePopoverPresentation.make(
                observation: observation,
                audit: ProtectionAuditSnapshot(),
                sessionLimit: 64
            )
            if case .stop = unavailable.lifecycleSlot.action {} else { XCTFail("unavailable Runtime should retain a Stop slot") }
            XCTAssertFalse(unavailable.lifecycleSlot.isEnabled)
        }
    }

    func testHealthyReadOnlyRuntimeIsPresentedAsServingWithoutLifecycleControl() {
        let observation = RuntimeObservation(status: .external([99]), tunnelTransport: .healthy)
        let menuIndicator = RuntimeStatusIndicator(observation: observation)
        let popover = RuntimePopoverPresentation.make(
            observation: observation,
            audit: ProtectionAuditSnapshot(),
            sessionLimit: 6
        )

        XCTAssertEqual(menuIndicator, .online)
        XCTAssertFalse(menuIndicator.appearsDisabled)
        XCTAssertEqual(popover.indicator, .online)
        XCTAssertEqual(popover.facts[0], RuntimeFact(label: "Endpoint", value: "127.0.0.1:8080"))
        XCTAssertEqual(popover.facts[2], RuntimeFact(label: "Health", value: "live"))
        XCTAssertEqual(popover.facts[3], RuntimeFact(label: "Ready", value: "ready"))
        XCTAssertEqual(popover.facts[4], RuntimeFact(label: "Tunnel", value: "Healthy"))
        XCTAssertFalse(popover.lifecycleSlot.isEnabled)
        XCTAssertTrue(popover.accessibilitySummary.contains("read-only"))
        XCTAssertFalse(popover.accessibilitySummary.contains("Connected"))
    }

    func testTunnelTransportAttentionDoesNotChangeLifecycleAuthority() {
        for tunnel in [TunnelTransportStatus.degraded, .unconfirmed] {
            let owned = RuntimeObservation(status: .owned(servingIdentity), tunnelTransport: tunnel)
            let ownedPresentation = RuntimePopoverPresentation.make(
                observation: owned,
                audit: ProtectionAuditSnapshot(),
                sessionLimit: 6
            )
            XCTAssertEqual(RuntimeStatusIndicator(observation: owned), .attention)
            XCTAssertEqual(ownedPresentation.indicator, .attention)
            XCTAssertEqual(ownedPresentation.facts[4].label, "Tunnel")
            XCTAssertEqual(
                ownedPresentation.facts[4].value,
                tunnel == .degraded ? "Degraded" : "Unconfirmed"
            )
            if case .stop = ownedPresentation.lifecycleSlot.action {} else {
                XCTFail("owned Runtime must keep Stop authority regardless of tunnel transport")
            }
            XCTAssertTrue(ownedPresentation.lifecycleSlot.isEnabled)

            let readOnly = RuntimeObservation(status: .external([99]), tunnelTransport: tunnel)
            let readOnlyPresentation = RuntimePopoverPresentation.make(
                observation: readOnly,
                audit: ProtectionAuditSnapshot(),
                sessionLimit: 6
            )
            XCTAssertEqual(RuntimeStatusIndicator(observation: readOnly), .attention)
            XCTAssertFalse(readOnlyPresentation.lifecycleSlot.isEnabled)
        }
    }

    func testPopoverHeaderUsesCircleStatusDotWithTruthfulSemantics() {
        let online = RuntimePopoverStatusIndicator(
            observation: RuntimeObservation(status: .owned(servingIdentity), tunnelTransport: .healthy)
        )
        XCTAssertEqual(online.symbolName, "circle.fill")
        XCTAssertEqual(online.color, .systemGreen)
        XCTAssertTrue(online.accessibilityLabel.contains("Online"))

        let attention = RuntimePopoverStatusIndicator(
            observation: RuntimeObservation(status: .owned(servingIdentity), tunnelTransport: .degraded)
        )
        XCTAssertEqual(attention.symbolName, "circle.fill")
        XCTAssertEqual(attention.color, .systemYellow)
        XCTAssertTrue(attention.accessibilityLabel.contains("Tunnel transport"))

        for observation in [
            RuntimeObservation(status: .stopped, tunnelTransport: .notRunning),
            RuntimeObservation(status: .ambiguous("health unavailable"), tunnelTransport: .unconfirmed),
        ] {
            let unavailable = RuntimePopoverStatusIndicator(observation: observation)
            XCTAssertEqual(unavailable.symbolName, "circle.fill")
            XCTAssertEqual(unavailable.color, .systemRed)
            XCTAssertTrue(unavailable.accessibilityLabel.contains("Offline or unconfirmed"))
        }
    }

    func testMenuBarStatusItemFollowsNativeAppearanceAndCommunicatesState() {
        let online = RuntimeStatusIndicator(
            observation: RuntimeObservation(status: .owned(servingIdentity), tunnelTransport: .healthy)
        )
        XCTAssertEqual(online, .online)
        XCTAssertEqual(online.symbolName, "bolt.horizontal.circle.fill")
        XCTAssertFalse(online.appearsDisabled)
        XCTAssertTrue(online.accessibilityLabel.contains("Serving"))

        let attention = RuntimeStatusIndicator(
            observation: RuntimeObservation(status: .owned(servingIdentity), tunnelTransport: .unconfirmed)
        )
        XCTAssertEqual(attention, .attention)
        XCTAssertEqual(attention.symbolName, "bolt.horizontal.circle")
        XCTAssertFalse(attention.appearsDisabled)
        XCTAssertTrue(attention.accessibilityLabel.contains("Tunnel transport"))

        for observation in [
            RuntimeObservation(status: .stopped, tunnelTransport: .notRunning),
            RuntimeObservation(status: .ambiguous("health unavailable"), tunnelTransport: .unconfirmed),
        ] {
            let offline = RuntimeStatusIndicator(observation: observation)
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

        let offlineImage = NSImage(
            systemSymbolName: RuntimeStatusIndicator(
                observation: RuntimeObservation(status: .stopped, tunnelTransport: .notRunning)
            ).symbolName,
            accessibilityDescription: nil
        )?
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
        let onlineObservation = RuntimeObservation(status: .owned(servingIdentity), tunnelTransport: .healthy)
        let onlineIndicator = RuntimeStatusIndicator(observation: onlineObservation)
        button.image = NSImage(systemSymbolName: onlineIndicator.symbolName, accessibilityDescription: onlineIndicator.accessibilityLabel)
        button.image?.isTemplate = true
        button.contentTintColor = nil
        button.appearsDisabled = onlineIndicator.appearsDisabled
        let onlineSummary = RuntimePopoverPresentation.accessibilitySummary(for: onlineObservation)
        button.toolTip = onlineSummary
        button.setAccessibilityValue(onlineSummary)

        XCTAssertFalse(button.appearsDisabled)
        XCTAssertNil(button.contentTintColor)
        XCTAssertTrue(button.isEnabled)
        XCTAssertEqual(button.toolTip, onlineSummary)

        // Simulate offline update
        let stoppedObservation = RuntimeObservation(status: .stopped, tunnelTransport: .notRunning)
        let offlineIndicator = RuntimeStatusIndicator(observation: stoppedObservation)
        button.image = NSImage(systemSymbolName: offlineIndicator.symbolName, accessibilityDescription: offlineIndicator.accessibilityLabel)
        button.image?.isTemplate = true
        button.contentTintColor = nil
        button.appearsDisabled = offlineIndicator.appearsDisabled
        let stoppedSummary = RuntimePopoverPresentation.accessibilitySummary(for: stoppedObservation)
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
        let healthy = RuntimePopoverPresentation.accessibilitySummary(
            for: RuntimeObservation(status: .owned(servingIdentity), tunnelTransport: .healthy)
        )
        XCTAssertFalse(healthy.contains("Connected"))
        XCTAssertTrue(healthy.contains("live and ready"))
        XCTAssertTrue(healthy.contains("tunnel transport healthy"))
        XCTAssertTrue(healthy.contains("ChatGPT connection is not proven"))

        let stopped = RuntimePopoverPresentation.accessibilitySummary(
            for: RuntimeObservation(status: .stopped, tunnelTransport: .notRunning)
        )
        XCTAssertTrue(stopped.contains("Offline"))
        XCTAssertTrue(stopped.contains("not serving"))

        let attention = RuntimePopoverPresentation.accessibilitySummary(
            for: RuntimeObservation(status: .owned(servingIdentity), tunnelTransport: .degraded)
        )
        XCTAssertTrue(attention.contains("Attention"))
        XCTAssertTrue(attention.contains("tunnel transport degraded"))
        XCTAssertTrue(attention.contains("ChatGPT connection is not proven"))
    }

    func testProtectionPresentationUsesRetainedBoundedHistoryWithoutRawCategory() {
        let presentation = RuntimePopoverPresentation.make(
            observation: RuntimeObservation(status: .owned(servingIdentity), tunnelTransport: .healthy),
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

    func testFirstRunDoctorGateRejectsObsoleteApprovalStateAndSurfacesPartialRecovery() throws {
        let obsoleteApproval = try doctorReport(
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
        XCTAssertEqual(FirstRunDoctorGate.preCommitDecision(from: obsoleteApproval), .blocked)

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

    func testFirstRunResumeStaysPendingWithoutApprovalAndUsesExistingCommitGatesAfterApproval() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        let paths = FirstRunSetupPaths(
            candidateApp: root.appendingPathComponent("release/Agent Runtime.app", isDirectory: true),
            home: root.appendingPathComponent("home", isDirectory: true)
        )
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
        let pendingRunner = RecordingFirstRunRunner(results: [
            .init(exitCode: 0),
            .init(exitCode: 1, standardOutput: approval),
        ])
        let pending = FirstRunSetupOrchestrator(paths: paths, runner: pendingRunner)

        XCTAssertEqual(
            pending.perform(.resume),
            .failure("Setup state is not safe to finalize. See recovery guidance for the bounded next action.")
        )
        XCTAssertEqual(pendingRunner.invocations.first?.arguments, [paths.installer.path, "--resume-cutover"])
        XCTAssertFalse(
            pendingRunner.invocations.contains(where: { $0.arguments.contains("--commit-cutover") })
        )

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
        let approvedRunner = RecordingFirstRunRunner(results: [
            .init(exitCode: 0),
            .init(exitCode: 1, standardOutput: preCommit),
            .init(exitCode: 0),
            .init(exitCode: 0, standardOutput: postCommit),
        ])
        let approved = FirstRunSetupOrchestrator(paths: paths, runner: approvedRunner)

        XCTAssertEqual(approved.perform(.resume), .success)
        XCTAssertEqual(approvedRunner.invocations.count, 4)
        XCTAssertEqual(approvedRunner.invocations[0].arguments, [paths.installer.path, "--resume-cutover"])
        XCTAssertEqual(approvedRunner.invocations[2].arguments, [paths.installer.path, "--commit-cutover"])
        XCTAssertFalse(approvedRunner.invocations.contains(where: { $0.executable.path == "/usr/bin/curl" }))
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
            .init(exitCode: 0, standardOutput: postCommit),
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
        XCTAssertEqual(runner.invocations.count, 5)
        XCTAssertEqual(runner.invocations[0].arguments.prefix(2), [paths.runtimeConfig.path, "--prebuilt-stdin"])
        XCTAssertEqual(runner.invocations[1].arguments, [paths.installer.path, "--workspace-root", workspace.path])
        XCTAssertEqual(runner.invocations[3].arguments, [paths.installer.path, "--commit-cutover"])
        XCTAssertFalse(runner.invocations.contains(where: { $0.executable.path == "/usr/bin/curl" }))
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
    func testCurrentFirstRunHasNoBackgroundActivityApprovalHandoff() throws {
        var recoveryActions: [FirstRunRecoveryAction] = []
        var completionCount = 0
        let controller = FirstRunSetupController(
            mode: .fresh,
            begin: { _ in .actionRequired("A partial cutover was detected.", .recover) },
            activateConfigured: { _ in .failure("not called") },
            performAction: { action in
                recoveryActions.append(action)
                return .failure("recovery complete")
            },
            completed: { completionCount += 1 }
        )
        controller.selectedWorkspace = URL(fileURLWithPath: "/")
        controller.loadView()

        try XCTUnwrap(button(titled: "Set Up", in: controller.view)).performClick(nil)

        XCTAssertFalse(
            labels(in: controller.view).contains(where: {
                $0.contains("Background Activity") || $0.contains("ServiceManagement")
            })
        )
        XCTAssertNil(button(titled: "Open Background Activity Settings…", in: controller.view))
        let recover = try XCTUnwrap(button(titled: "Recover", in: controller.view))
        recover.performClick(nil)
        XCTAssertEqual(recoveryActions, [.recover])
        XCTAssertEqual(completionCount, 0)
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

    @MainActor
    private func button(titled title: String, in view: NSView) -> NSButton? {
        if let button = view as? NSButton, button.title == title {
            return button
        }
        for subview in view.subviews {
            if let match = button(titled: title, in: subview) {
                return match
            }
        }
        return nil
    }

    @MainActor
    private func labels(in view: NSView) -> [String] {
        var values: [String] = []
        if let field = view as? NSTextField, !field.stringValue.isEmpty {
            values.append(field.stringValue)
        }
        for subview in view.subviews {
            values.append(contentsOf: labels(in: subview))
        }
        return values
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
