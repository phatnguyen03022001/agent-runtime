import AgentRuntimeCore
import AppKit
import Foundation

enum RuntimePresentationCondition: Equatable {
    case connected
    case offline
    case external
    case attention

    init(status: RuntimeStatus) {
        switch status {
        case .owned: self = .connected
        case .stopped: self = .offline
        case .external: self = .external
        case .ambiguous: self = .attention
        }
    }
}

struct OfflineAudioPolicy {
    private var lastCondition: RuntimePresentationCondition?
    private var hasObservedConnected = false

    mutating func shouldPlay(for status: RuntimeStatus) -> Bool {
        let nextCondition = RuntimePresentationCondition(status: status)
        let nextIsOffline = nextCondition == .offline || nextCondition == .attention
        let wasOffline = lastCondition == .offline || lastCondition == .attention
        let shouldPlay = hasObservedConnected
            && nextIsOffline
            && !wasOffline

        if nextCondition == .connected {
            hasObservedConnected = true
        }
        lastCondition = nextCondition
        return shouldPlay
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private let runtimeQueue = DispatchQueue(label: "com.picmao.agent-runtime.lifecycle", qos: .userInitiated)
    private let popover = NSPopover()
    private var statusItem: NSStatusItem!
    private var timer: Timer?
    private var controller: RuntimeController?
    private var runtimeConfiguration: RuntimeConfiguration?
    private var configurationError: String?
    private var instanceLock: MenuBarInstanceLock?
    private var offlineAudioPolicy = OfflineAudioPolicy()
    private var notificationSound: NSSound?
    private let auditReader = ProtectionAuditReader()
    private lazy var controlPanel = makeControlPanel()

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard let lock = MenuBarInstanceLock() else {
            // launchd may observe a second manual/duplicate launch. The lock
            // makes that process exit without creating a second status item.
            NSApp.terminate(nil)
            return
        }
        instanceLock = lock
        NSApp.setActivationPolicy(.accessory)
        configureRuntime()
        configureStatusItem()
        configurePopover()
        refreshStatus()
        timer = Timer.scheduledTimer(
            timeInterval: 2.0,
            target: self,
            selector: #selector(timerFired),
            userInfo: nil,
            repeats: true
        )
    }

    func applicationWillTerminate(_ notification: Notification) {
        timer?.invalidate()
        instanceLock = nil
    }

    func applicationDidResignActive(_ notification: Notification) {
        if popover.isShown { popover.performClose(nil) }
    }

    private func configureRuntime() {
        do {
            let configuration = try Self.installedRuntimeConfiguration()
            let system = DarwinProcessSystem()
            let backend = NativeRuntimeBackend(
                configuration: configuration,
                inspector: system,
                discovery: PSRuntimeDiscovery(inspector: system)
            )
            runtimeConfiguration = configuration
            controller = RuntimeController(backend: backend)
        } catch {
            configurationError = error.localizedDescription
        }
    }

    private static func installedRuntimeConfiguration() throws -> RuntimeConfiguration {
        guard let resources = Bundle.main.resourceURL else {
            throw RuntimeLifecycleError.metadata("app bundle resources are unavailable")
        }
        let manifestURL = resources.appendingPathComponent("runtime-manifest.json", isDirectory: false)
        let manifestData = try Data(contentsOf: manifestURL)
        guard let manifest = try JSONSerialization.jsonObject(with: manifestData) as? [String: Any],
              manifest["schema"] as? Int == 1,
              manifest["owner"] as? String == "com.picmao.agent-runtime",
              manifest["entrypoint"] as? String == "runtime/start.sh",
              manifest["python"] as? String == "runtime/.venv/bin/python",
              let revision = manifest["runtime_revision"] as? String,
              revision.count == 40 else {
            throw RuntimeLifecycleError.metadata("installed Runtime manifest is invalid")
        }

        let runtimeRoot = resources.appendingPathComponent("runtime", isDirectory: true)
        let lifecycle = runtimeRoot.appendingPathComponent("start.sh", isDirectory: false)
        let runtimePython = runtimeRoot.appendingPathComponent(".venv/bin/python", isDirectory: false)
        let server = runtimeRoot.appendingPathComponent("agent_runtime/server.py", isDirectory: false)
        let runtimeValues = try runtimeRoot.resourceValues(forKeys: [.isSymbolicLinkKey])
        guard runtimeValues.isSymbolicLink != true,
              FileManager.default.isExecutableFile(atPath: lifecycle.path),
              FileManager.default.isExecutableFile(atPath: runtimePython.path),
              FileManager.default.fileExists(atPath: server.path) else {
            throw RuntimeLifecycleError.metadata("installed Runtime payload is incomplete")
        }

        let envURL = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Agent Runtime/runtime.env", isDirectory: false)
        let values = try envURL.resourceValues(forKeys: [.isSymbolicLinkKey, .isRegularFileKey])
        guard values.isSymbolicLink != true, values.isRegularFile == true else {
            throw RuntimeLifecycleError.metadata("canonical Runtime configuration is unavailable")
        }
        let attributes = try FileManager.default.attributesOfItem(atPath: envURL.path)
        guard let permissions = attributes[.posixPermissions] as? NSNumber,
              permissions.uint16Value == 0o600 else {
            throw RuntimeLifecycleError.metadata("canonical Runtime configuration must have mode 0600")
        }

        return RuntimeConfiguration(
            runtimeRoot: runtimeRoot.path,
            envFileURL: envURL,
            requiresReadiness: true
        )
    }

    private func makeControlPanel() -> ControlPanelController {
        ControlPanelController(
            performAction: { [weak self] action in self?.perform(action) },
            quit: { NSApp.terminate(nil) }
        )
    }

    private func configureStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        guard let button = statusItem.button else { return }
        button.target = self
        button.action = #selector(togglePopover)
        button.setAccessibilityLabel("Agent Runtime status")
        updateStatusItem(for: .ambiguous("Refreshing Runtime status…"))
    }

    private func configurePopover() {
        popover.behavior = .transient
        popover.animates = false
        popover.contentViewController = controlPanel
    }

    @objc private func timerFired() {
        refreshStatus()
    }

    @objc private func togglePopover() {
        guard let button = statusItem.button else { return }
        if popover.isShown {
            popover.performClose(nil)
            return
        }
        refreshStatus()
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func refreshStatus() {
        let sessionLimit = runtimeConfiguration?.sessionLimit ?? RuntimeSessionCapacity.fallback
        guard let controller else {
            let status = RuntimeStatus.ambiguous(configurationError ?? "Configuration unavailable")
            present(status, sessionLimit: sessionLimit)
            return
        }
        runtimeQueue.async { [weak self, controller] in
            let status = controller.refresh()
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.present(status)
            }
        }
    }

    private func perform(_ action: RuntimeAction) {
        guard let controller else { return }
        controlPanel.setBusy(action)
        runtimeQueue.async { [weak self, controller] in
            let result: Result<RuntimeStatus, RuntimeLifecycleError>
            switch action {
            case .start: result = controller.start()
            case .stop: result = controller.stop()
            case .restart: result = controller.restart()
            }
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                switch result {
                case .success(let status):
                    self.present(status)
                case .failure(let error):
                    let status = controller.refresh()
                    self.present(status)
                    self.showError(error.localizedDescription)
                }
            }
        }
    }

    private func present(
        _ status: RuntimeStatus,
        sessionLimit: Int? = nil
    ) {
        controlPanel.apply(
            status: status,
            audit: auditReader.read(),
            sessionLimit: sessionLimit ?? runtimeConfiguration?.sessionLimit ?? RuntimeSessionCapacity.fallback
        )
        updateStatusItem(for: status)
        playOfflineNotificationIfNeeded(for: status)
    }

    private func playOfflineNotificationIfNeeded(for status: RuntimeStatus) {
        guard offlineAudioPolicy.shouldPlay(for: status) else { return }
        if notificationSound == nil,
           let url = Bundle.main.url(forResource: "notification", withExtension: "mp3") {
            notificationSound = NSSound(contentsOf: url, byReference: false)
        }
        notificationSound?.stop()
        notificationSound?.play()
    }

    private func updateStatusItem(for status: RuntimeStatus) {
        guard let button = statusItem.button else { return }
        let indicator = RuntimeStatusIndicator(status: status)
        let configuration = NSImage.SymbolConfiguration(pointSize: 13, weight: .medium)
        let image = NSImage(systemSymbolName: indicator.symbolName, accessibilityDescription: indicator.accessibilityLabel)?
            .withSymbolConfiguration(configuration)
        image?.isTemplate = true
        button.image = image
        button.contentTintColor = indicator.color
        let summary = RuntimePopoverPresentation.accessibilitySummary(for: status)
        button.toolTip = summary
        button.setAccessibilityValue(summary)
    }

    private func showError(_ message: String) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Agent Runtime"
        alert.informativeText = message
        alert.runModal()
    }
}
