import AgentRuntimeCore
import AppKit

@MainActor
final class ControlPanelController: NSViewController {
    private let performAction: (RuntimeAction) -> Void
    private let quit: () -> Void
    private let statusLabel = NSTextField(labelWithString: "Checking…")
    private let statusDetailLabel = NSTextField(labelWithString: "Refreshing Runtime status…")
    private let endpointLabel = NSTextField(labelWithString: "Endpoint unavailable")
    private let healthLabel = NSTextField(labelWithString: "Health — · Ready —")
    private let capacityLabel = NSTextField(labelWithString: "Session capacity 64")
    private let protectionLabel = NSTextField(labelWithString: "Protection: clear")
    private let startButton = NSButton()
    private let stopButton = NSButton()
    private let restartButton = NSButton()

    init(performAction: @escaping (RuntimeAction) -> Void, quit: @escaping () -> Void) {
        self.performAction = performAction
        self.quit = quit
        super.init(nibName: nil, bundle: nil)
        preferredContentSize = NSSize(width: 304, height: 224)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func loadView() {
        view = NSView(frame: NSRect(x: 0, y: 0, width: 304, height: 224))
        view.setAccessibilityLabel("Agent Runtime controls")

        let title = NSTextField(labelWithString: "Agent Runtime")
        title.font = .systemFont(ofSize: NSFont.systemFontSize, weight: .semibold)
        title.textColor = .labelColor

        statusLabel.font = .systemFont(ofSize: 15, weight: .semibold)
        statusLabel.textColor = .labelColor
        statusDetailLabel.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        statusDetailLabel.textColor = .secondaryLabelColor
        statusDetailLabel.lineBreakMode = .byTruncatingTail
        endpointLabel.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        endpointLabel.textColor = .secondaryLabelColor
        endpointLabel.lineBreakMode = .byTruncatingTail
        healthLabel.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        healthLabel.textColor = .secondaryLabelColor
        capacityLabel.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        capacityLabel.textColor = .secondaryLabelColor
        protectionLabel.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        protectionLabel.textColor = .secondaryLabelColor
        protectionLabel.lineBreakMode = .byTruncatingTail

        setAccessibilityLabel("Runtime state", on: statusLabel)
        setAccessibilityLabel("Runtime state detail", on: statusDetailLabel)
        setAccessibilityLabel("Runtime endpoint", on: endpointLabel)
        setAccessibilityLabel("Runtime health", on: healthLabel)
        setAccessibilityLabel("Session capacity", on: capacityLabel)
        setAccessibilityLabel("Protection audit", on: protectionLabel)

        let statusStack = NSStackView(views: [statusLabel, statusDetailLabel])
        statusStack.orientation = .vertical
        statusStack.alignment = .leading
        statusStack.spacing = 2

        let factsStack = NSStackView(views: [endpointLabel, healthLabel])
        factsStack.orientation = .vertical
        factsStack.alignment = .leading
        factsStack.spacing = 2

        configure(startButton, title: "Start", action: #selector(startPressed))
        configure(stopButton, title: "Stop", action: #selector(stopPressed))
        configure(restartButton, title: "Restart", action: #selector(restartPressed))
        startButton.isEnabled = false
        stopButton.isEnabled = false
        restartButton.isEnabled = false
        let controls = NSStackView(views: [startButton, stopButton, restartButton])
        controls.orientation = .horizontal
        controls.alignment = .centerY
        controls.distribution = .fillEqually
        controls.spacing = 6
        controls.setAccessibilityLabel("Runtime lifecycle actions")

        let quitButton = NSButton(title: "Quit Agent Runtime", target: self, action: #selector(quitPressed))
        quitButton.isBordered = false
        quitButton.bezelStyle = .inline
        quitButton.controlSize = .small
        quitButton.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        quitButton.contentTintColor = .secondaryLabelColor
        quitButton.setAccessibilityLabel("Quit Agent Runtime")

        let divider = separator()
        let root = NSStackView(views: [title, statusStack, factsStack, capacityLabel, protectionLabel, controls, divider, quitButton])
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 7
        root.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(root)
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 14),
            root.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -14),
            root.topAnchor.constraint(equalTo: view.topAnchor, constant: 13),
            root.bottomAnchor.constraint(equalTo: view.bottomAnchor, constant: -10),
            controls.widthAnchor.constraint(equalTo: root.widthAnchor),
            divider.widthAnchor.constraint(equalTo: root.widthAnchor),
        ])
    }

    func apply(
        status: RuntimeStatus,
        audit: ProtectionAuditSnapshot = ProtectionAuditSnapshot(),
        sessionLimit: Int = RuntimeSessionCapacity.fallback
    ) {
        let availability = RuntimePolicy.actions(for: status)
        startButton.isEnabled = availability.canStart
        stopButton.isEnabled = availability.canStop
        restartButton.isEnabled = availability.canRestart
        capacityLabel.stringValue = "Session capacity \(sessionLimit)"
        if audit.blockedCount > 0 {
            let category = audit.lastCategory ?? "protected lifecycle"
            protectionLabel.stringValue = "Protection: \(audit.blockedCount) blocked · last: \(category)"
            protectionLabel.textColor = .systemOrange
        } else {
            protectionLabel.stringValue = "Protection: clear"
            protectionLabel.textColor = .secondaryLabelColor
        }
        switch status {
        case .stopped:
            setState(status: "STOPPED", detail: "Desired state STOPPED · recovery suppressed.", endpoint: "Endpoint unavailable", health: "Health — · Ready —")
        case .owned(let identity):
            setState(status: "RUNNING", detail: "Owned by Agent Runtime", endpoint: "Endpoint 127.0.0.1:8080 · PID \(identity.pid)", health: "Health live · Ready ready")
        case .external(let pids):
            let identities = pids.map(String.init).joined(separator: ", ")
            setState(status: "RUNNING · EXTERNAL", detail: "Read-only · lifecycle controls disabled.", endpoint: "Endpoint 127.0.0.1:8080 · PID(s) \(identities)", health: "Health unverified · Ready unverified")
        case .ambiguous(let message):
            setState(status: "UNAVAILABLE", detail: message, endpoint: "Endpoint unavailable", health: "Health unverified · Ready unverified")
        }
        refreshAccessibilityValues()
    }

    func setBusy(_ action: RuntimeAction) {
        startButton.isEnabled = false
        stopButton.isEnabled = false
        restartButton.isEnabled = false
        switch action {
        case .start:
            statusLabel.stringValue = "STARTING…"
            statusDetailLabel.stringValue = "Explicit operator action in progress."
        case .stop:
            statusLabel.stringValue = "STOPPING…"
            statusDetailLabel.stringValue = "Setting desired state STOPPED and stopping the singleton."
        case .restart:
            statusLabel.stringValue = "RESTARTING…"
            statusDetailLabel.stringValue = "Restarting the supervised singleton with desired state RUNNING."
        }
        endpointLabel.stringValue = "Endpoint pending"
        healthLabel.stringValue = "Health pending · Ready pending"
        refreshAccessibilityValues()
    }

    private func configure(_ button: NSButton, title: String, action: Selector) {
        button.title = title
        button.target = self
        button.action = action
        button.bezelStyle = .rounded
        button.controlSize = .regular
        button.setAccessibilityLabel("\(title) Agent Runtime")
    }

    private func separator() -> NSBox {
        let box = NSBox()
        box.boxType = .separator
        box.translatesAutoresizingMaskIntoConstraints = false
        return box
    }

    private func setState(status: String, detail: String, endpoint: String, health: String) {
        statusLabel.stringValue = status
        statusDetailLabel.stringValue = detail
        endpointLabel.stringValue = endpoint
        healthLabel.stringValue = health
    }

    private func setAccessibilityLabel(_ label: String, on field: NSTextField) {
        field.setAccessibilityLabel(label)
        field.setAccessibilityValue(field.stringValue)
    }

    private func refreshAccessibilityValues() {
        statusLabel.setAccessibilityValue(statusLabel.stringValue)
        statusDetailLabel.setAccessibilityValue(statusDetailLabel.stringValue)
        endpointLabel.setAccessibilityValue(endpointLabel.stringValue)
        healthLabel.setAccessibilityValue(healthLabel.stringValue)
        capacityLabel.setAccessibilityValue(capacityLabel.stringValue)
        protectionLabel.setAccessibilityValue(protectionLabel.stringValue)
    }

    @objc private func startPressed() { performAction(.start) }
    @objc private func stopPressed() { performAction(.stop) }
    @objc private func restartPressed() { performAction(.restart) }
    @objc private func quitPressed() { quit() }
}
