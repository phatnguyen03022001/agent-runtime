import AgentRuntimeCore
import AppKit

@MainActor
final class ControlPanelController: NSViewController {
    private let performAction: (RuntimeAction) -> Void
    private let quit: () -> Void
    private let statusLabel = NSTextField(labelWithString: "Stopped")
    private let detailLabel = NSTextField(labelWithString: "")
    private let capacityLabel = NSTextField(labelWithString: "Persistent sessions: 64")
    private let protectionLabel = NSTextField(labelWithString: "Protection: no blocked attempts")
    private let startButton = NSButton()
    private let stopButton = NSButton()
    private let restartButton = NSButton()

    init(performAction: @escaping (RuntimeAction) -> Void, quit: @escaping () -> Void) {
        self.performAction = performAction
        self.quit = quit
        super.init(nibName: nil, bundle: nil)
        preferredContentSize = NSSize(width: 320, height: 214)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func loadView() {
        view = NSView(frame: NSRect(x: 0, y: 0, width: 320, height: 214))

        let title = NSTextField(labelWithString: "Agent Runtime")
        title.font = .systemFont(ofSize: NSFont.systemFontSize, weight: .semibold)
        title.textColor = .labelColor

        statusLabel.font = .systemFont(ofSize: NSFont.systemFontSize, weight: .medium)
        statusLabel.textColor = .labelColor
        detailLabel.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        detailLabel.textColor = .secondaryLabelColor
        detailLabel.lineBreakMode = .byTruncatingTail
        capacityLabel.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        capacityLabel.textColor = .secondaryLabelColor
        protectionLabel.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        protectionLabel.textColor = .secondaryLabelColor
        protectionLabel.lineBreakMode = .byTruncatingTail

        let statusStack = NSStackView(views: [statusLabel, detailLabel])
        statusStack.orientation = .vertical
        statusStack.alignment = .leading
        statusStack.spacing = 1

        configure(startButton, title: "Start", action: #selector(startPressed))
        configure(stopButton, title: "Stop", action: #selector(stopPressed))
        configure(restartButton, title: "Restart", action: #selector(restartPressed))
        let controls = NSStackView(views: [startButton, stopButton, restartButton])
        controls.orientation = .horizontal
        controls.alignment = .centerY
        controls.spacing = 8

        let quitButton = NSButton(title: "Quit Agent Runtime", target: self, action: #selector(quitPressed))
        quitButton.isBordered = false
        quitButton.bezelStyle = .inline
        quitButton.controlSize = .small
        quitButton.font = .systemFont(ofSize: NSFont.smallSystemFontSize)

        let root = NSStackView(views: [title, statusStack, capacityLabel, protectionLabel, controls, separator(), quitButton])
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 10
        root.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(root)
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 16),
            root.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -16),
            root.topAnchor.constraint(equalTo: view.topAnchor, constant: 14),
            root.bottomAnchor.constraint(equalTo: view.bottomAnchor, constant: -12),
            controls.widthAnchor.constraint(equalTo: root.widthAnchor),
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
        capacityLabel.stringValue = "Persistent sessions: \(sessionLimit)"
        if audit.blockedCount > 0 {
            let category = audit.lastCategory ?? "protected lifecycle"
            protectionLabel.stringValue = "Protection: \(audit.blockedCount) blocked · last: \(category)"
            protectionLabel.textColor = .systemOrange
        } else {
            protectionLabel.stringValue = "Protection: no blocked attempts"
            protectionLabel.textColor = .secondaryLabelColor
        }
        switch status {
        case .stopped:
            statusLabel.stringValue = "Stopped"
            detailLabel.stringValue = "Desired state STOPPED · supervisor recovery is suppressed."
        case .owned:
            statusLabel.stringValue = "Running"
            detailLabel.stringValue = "Protected singleton · supervisor recovery is active."
        case .external:
            statusLabel.stringValue = "Running externally"
            detailLabel.stringValue = "Read-only · lifecycle controls are disabled."
        case .ambiguous(let message):
            statusLabel.stringValue = "Unavailable"
            detailLabel.stringValue = message
        }
    }

    func setBusy(_ action: RuntimeAction) {
        startButton.isEnabled = false
        stopButton.isEnabled = false
        restartButton.isEnabled = false
        switch action {
        case .start:
            statusLabel.stringValue = "Starting…"
            detailLabel.stringValue = "Explicit operator action in progress."
        case .stop:
            statusLabel.stringValue = "Stopping…"
            detailLabel.stringValue = "Setting desired state STOPPED and stopping the singleton."
        case .restart:
            statusLabel.stringValue = "Restarting…"
            detailLabel.stringValue = "Restarting the supervised singleton with desired state RUNNING."
        }
    }

    private func configure(_ button: NSButton, title: String, action: Selector) {
        button.title = title
        button.target = self
        button.action = action
        button.bezelStyle = .rounded
        button.controlSize = .regular
    }

    private func separator() -> NSBox {
        let box = NSBox()
        box.boxType = .separator
        box.translatesAutoresizingMaskIntoConstraints = false
        box.widthAnchor.constraint(equalToConstant: 268).isActive = true
        return box
    }

    @objc private func startPressed() { performAction(.start) }
    @objc private func stopPressed() { performAction(.stop) }
    @objc private func restartPressed() { performAction(.restart) }
    @objc private func quitPressed() { quit() }
}
