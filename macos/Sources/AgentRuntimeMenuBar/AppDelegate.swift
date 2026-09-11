import AgentRuntimeCore
import AppKit

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private let runtimeQueue = DispatchQueue(label: "com.picmao.agent-runtime.lifecycle", qos: .userInitiated)
    private let popover = NSPopover()
    private var statusItem: NSStatusItem!
    private var timer: Timer?
    private var controller: RuntimeController?
    private var configurationError: String?
    private lazy var controlPanel = makeControlPanel()

    func applicationDidFinishLaunching(_ notification: Notification) {
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
    }

    func applicationDidResignActive(_ notification: Notification) {
        if popover.isShown { popover.performClose(nil) }
    }

    private func configureRuntime() {
        do {
            let checkoutRoot = try Self.checkoutRoot()
            let system = DarwinProcessSystem()
            let store = FileOwnershipStore()
            let launcher = POSIXProcessLauncher(inspector: system, signaler: system)
            let supervisor = OwnedProcessSupervisor(
                inspector: system,
                signaler: system,
                store: store,
                launcher: launcher
            )
            let backend = NativeRuntimeBackend(
                configuration: RuntimeConfiguration(checkoutRoot: checkoutRoot),
                inspector: system,
                store: store,
                supervisor: supervisor,
                discovery: PSRuntimeDiscovery(inspector: system)
            )
            controller = RuntimeController(backend: backend)
        } catch {
            configurationError = error.localizedDescription
        }
    }

    private static func checkoutRoot() throws -> String {
        guard let resources = Bundle.main.resourceURL else {
            throw RuntimeLifecycleError.metadata("app bundle resources are unavailable")
        }
        let pointer = resources.appendingPathComponent("checkout-path.txt", isDirectory: false)
        let text = try String(contentsOf: pointer, encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else {
            throw RuntimeLifecycleError.metadata("checkout path is empty")
        }
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: text, isDirectory: &isDirectory), isDirectory.boolValue else {
            throw RuntimeLifecycleError.metadata("configured checkout no longer exists")
        }
        return URL(fileURLWithPath: text).standardizedFileURL.path
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
        button.toolTip = "Agent Runtime"
        updateStatusItem(for: .stopped)
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
        guard let controller else {
            let status = RuntimeStatus.ambiguous(configurationError ?? "Configuration unavailable")
            controlPanel.apply(status: status)
            updateStatusItem(for: status)
            return
        }
        runtimeQueue.async { [weak self, controller] in
            let status = controller.refresh()
            DispatchQueue.main.async { [weak self] in
                self?.controlPanel.apply(status: status)
                self?.updateStatusItem(for: status)
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
                    self.controlPanel.apply(status: status)
                    self.updateStatusItem(for: status)
                case .failure(let error):
                    let status = controller.refresh()
                    self.controlPanel.apply(status: status)
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
    }

    private func showError(_ message: String) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Agent Runtime"
        alert.informativeText = message
        alert.runModal()
    }
}
