import AgentRuntimeCore
import AppKit
import Foundation

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

        let pointer = resources.appendingPathComponent("env-path.txt", isDirectory: false)
        let pointerValues = try pointer.resourceValues(forKeys: [.isSymbolicLinkKey])
        guard pointerValues.isSymbolicLink != true else {
            throw RuntimeLifecycleError.metadata("installed .env pointer is a symlink")
        }
        let envPath = try String(contentsOf: pointer, encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let envURL = URL(fileURLWithPath: envPath)
        guard envURL.path.hasPrefix("/"), envURL.lastPathComponent == ".env",
              FileManager.default.fileExists(atPath: envURL.path) else {
            throw RuntimeLifecycleError.metadata("checkout-local .env authority is unavailable")
        }
        let values = try envURL.resourceValues(forKeys: [.isSymbolicLinkKey, .isRegularFileKey])
        guard values.isSymbolicLink != true, values.isRegularFile == true else {
            throw RuntimeLifecycleError.metadata("checkout-local .env must be a regular non-symlink file")
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
            controlPanel.apply(status: status, audit: auditReader.read(), sessionLimit: sessionLimit)
            updateStatusItem(for: status)
            return
        }
        runtimeQueue.async { [weak self, controller] in
            let status = controller.refresh()
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.controlPanel.apply(
                    status: status,
                    audit: self.auditReader.read(),
                    sessionLimit: self.runtimeConfiguration?.sessionLimit ?? RuntimeSessionCapacity.fallback
                )
                self.updateStatusItem(for: status)
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
                    self.controlPanel.apply(
                        status: status,
                        audit: self.auditReader.read(),
                        sessionLimit: self.runtimeConfiguration?.sessionLimit ?? RuntimeSessionCapacity.fallback
                    )
                    self.updateStatusItem(for: status)
                case .failure(let error):
                    let status = controller.refresh()
                    self.controlPanel.apply(
                        status: status,
                        audit: self.auditReader.read(),
                        sessionLimit: self.runtimeConfiguration?.sessionLimit ?? RuntimeSessionCapacity.fallback
                    )
                    self.updateStatusItem(for: status)
                    self.showError(error.localizedDescription)
                }
            }
        }
    }

    private func updateStatusItem(for status: RuntimeStatus) {
        guard let button = statusItem.button else { return }
        let symbol: String
        switch status {
        case .owned: symbol = "bolt.horizontal.circle.fill"
        case .external: symbol = "lock.circle"
        case .stopped: symbol = "bolt.horizontal.circle"
        case .ambiguous: symbol = "exclamationmark.circle"
        }
        let configuration = NSImage.SymbolConfiguration(pointSize: 13, weight: .medium)
        let image = NSImage(systemSymbolName: symbol, accessibilityDescription: "Agent Runtime")?
            .withSymbolConfiguration(configuration)
        image?.isTemplate = true
        button.image = image
        let summary = accessibilitySummary(for: status)
        button.toolTip = summary
        button.setAccessibilityValue(summary)
    }

    private func accessibilitySummary(for status: RuntimeStatus) -> String {
        switch status {
        case .stopped:
            return "Stopped; desired state STOPPED"
        case .owned(let identity):
            return "Running; endpoint 127.0.0.1:8080; PID \(identity.pid); live and ready"
        case .external(let pids):
            let identities = pids.map(String.init).joined(separator: ", ")
            return "Running externally; endpoint 127.0.0.1:8080; PID(s) \(identities); read-only"
        case .ambiguous(let message):
            return "Unavailable; \(message)"
        }
    }

    private func showError(_ message: String) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Agent Runtime"
        alert.informativeText = message
        alert.runModal()
    }
}
