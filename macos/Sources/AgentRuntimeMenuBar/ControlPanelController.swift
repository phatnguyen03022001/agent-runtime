import AgentRuntimeCore
import AppKit

struct RuntimeFact: Equatable {
    let label: String
    let value: String
}

enum RuntimeStatusIndicator: Equatable {
    case green
    case red

    init(status: RuntimeStatus) {
        if case .owned = status {
            self = .green
        } else {
            self = .red
        }
    }

    var symbolName: String { "circle.fill" }

    var color: NSColor {
        switch self {
        case .green: return .systemGreen
        case .red: return .systemRed
        }
    }

    var accessibilityLabel: String {
        switch self {
        case .green: return "Serving; health live and readiness ready"
        case .red: return "Not serving or health and readiness are unconfirmed"
        }
    }
}

enum RuntimeFactLayout {
    static let labelColumn = 0
    static let valueColumn = 1
    static let rowSpacing: CGFloat = 5
    static let columnSpacing: CGFloat = 12
    static let valueAlignment: NSTextAlignment = .right
}

struct RuntimePopoverPresentation {
    let indicator: RuntimeStatusIndicator
    let accessibilitySummary: String
    let facts: [RuntimeFact]
    let lifecycleAction: RuntimeAction?

    static func make(
        status: RuntimeStatus,
        audit: ProtectionAuditSnapshot,
        sessionLimit: Int
    ) -> RuntimePopoverPresentation {
        let availability = RuntimePolicy.actions(for: status)
        let lifecycleAction: RuntimeAction?
        if availability.canStop {
            lifecycleAction = .stop
        } else if availability.canStart {
            lifecycleAction = .start
        } else {
            lifecycleAction = nil
        }

        let protection = audit.blockedCount == 0 ? "Clear" : "\(audit.blockedCount) retained"
        let facts: [RuntimeFact]
        switch status {
        case .stopped:
            facts = [
                RuntimeFact(label: "Endpoint", value: "Unavailable"),
                RuntimeFact(label: "PID", value: "—"),
                RuntimeFact(label: "Health", value: "—"),
                RuntimeFact(label: "Ready", value: "—"),
                RuntimeFact(label: "Sessions", value: "\(sessionLimit) max"),
                RuntimeFact(label: "Protection", value: protection),
            ]
        case .owned(let identity):
            facts = [
                RuntimeFact(label: "Endpoint", value: "127.0.0.1:8080"),
                RuntimeFact(label: "PID", value: "\(identity.pid)"),
                RuntimeFact(label: "Health", value: "live"),
                RuntimeFact(label: "Ready", value: "ready"),
                RuntimeFact(label: "Sessions", value: "\(sessionLimit) max"),
                RuntimeFact(label: "Protection", value: protection),
            ]
        case .external(let pids):
            let identities = pids.map(String.init).joined(separator: ", ")
            facts = [
                RuntimeFact(label: "Endpoint", value: "127.0.0.1:8080"),
                RuntimeFact(label: "PID", value: "External \(identities)"),
                RuntimeFact(label: "Health", value: "Unverified"),
                RuntimeFact(label: "Ready", value: "Unverified"),
                RuntimeFact(label: "Sessions", value: "\(sessionLimit) max"),
                RuntimeFact(label: "Protection", value: protection),
            ]
        case .ambiguous:
            facts = [
                RuntimeFact(label: "Endpoint", value: "Unavailable"),
                RuntimeFact(label: "PID", value: "—"),
                RuntimeFact(label: "Health", value: "Unverified"),
                RuntimeFact(label: "Ready", value: "Unverified"),
                RuntimeFact(label: "Sessions", value: "\(sessionLimit) max"),
                RuntimeFact(label: "Protection", value: protection),
            ]
        }

        return RuntimePopoverPresentation(
            indicator: RuntimeStatusIndicator(status: status),
            accessibilitySummary: accessibilitySummary(for: status),
            facts: facts,
            lifecycleAction: lifecycleAction
        )
    }

    static func accessibilitySummary(for status: RuntimeStatus) -> String {
        switch status {
        case .stopped:
            return "Offline; desired state STOPPED; not serving."
        case .owned(let identity):
            return "Connected; endpoint 127.0.0.1:8080; PID \(identity.pid); live and ready."
        case .external(let pids):
            let identities = pids.map(String.init).joined(separator: ", ")
            return "External; endpoint 127.0.0.1:8080; PID(s) \(identities); read-only; health and readiness unverified."
        case .ambiguous(let message):
            return "Attention; unavailable; \(message)"
        }
    }
}

@MainActor
final class ControlPanelController: NSViewController {
    private let performAction: (RuntimeAction) -> Void
    private let quit: () -> Void
    private let statusIndicatorView = NSImageView()
    private let lifecycleButton = NSButton()
    private let divider = NSBox()
    private var factsGrid: NSGridView!
    private var factValueLabels: [NSTextField] = []
    private var lifecycleAction: RuntimeAction?
    private var rootStack: NSStackView!

    init(performAction: @escaping (RuntimeAction) -> Void, quit: @escaping () -> Void) {
        self.performAction = performAction
        self.quit = quit
        super.init(nibName: nil, bundle: nil)
        preferredContentSize = NSSize(width: 304, height: 1)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func loadView() {
        view = NSView(frame: NSRect(x: 0, y: 0, width: 304, height: 1))
        view.setAccessibilityLabel("Agent Runtime controls")

        let title = NSTextField(labelWithString: "Agent Runtime")
        title.font = .systemFont(ofSize: NSFont.systemFontSize, weight: .semibold)
        title.textColor = .labelColor
        title.setContentHuggingPriority(.defaultLow, for: .horizontal)

        statusIndicatorView.imageScaling = .scaleProportionallyDown
        statusIndicatorView.setContentHuggingPriority(.required, for: .horizontal)
        statusIndicatorView.setContentCompressionResistancePriority(.required, for: .horizontal)
        statusIndicatorView.translatesAutoresizingMaskIntoConstraints = false
        statusIndicatorView.widthAnchor.constraint(equalToConstant: 14).isActive = true
        statusIndicatorView.heightAnchor.constraint(equalToConstant: 14).isActive = true
        statusIndicatorView.setAccessibilityLabel("Runtime status")

        let header = NSStackView(views: [title, statusIndicatorView])
        header.orientation = .horizontal
        header.alignment = .centerY
        header.distribution = .fill
        header.spacing = 8

        factsGrid = makeFactsGrid()

        configure(lifecycleButton, title: "", action: #selector(lifecyclePressed))
        lifecycleButton.isHidden = true
        lifecycleButton.isEnabled = false

        let quitButton = NSButton(title: "Quit Agent Runtime", target: self, action: #selector(quitPressed))
        quitButton.isBordered = false
        quitButton.bezelStyle = .inline
        quitButton.controlSize = .small
        quitButton.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        quitButton.contentTintColor = .secondaryLabelColor
        quitButton.setAccessibilityLabel("Quit Agent Runtime")

        divider.boxType = .separator
        divider.translatesAutoresizingMaskIntoConstraints = false

        rootStack = NSStackView(views: [header, factsGrid, lifecycleButton, divider, quitButton])
        rootStack.orientation = .vertical
        rootStack.alignment = .leading
        rootStack.spacing = 8
        rootStack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(rootStack)
        NSLayoutConstraint.activate([
            rootStack.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 14),
            rootStack.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -14),
            rootStack.topAnchor.constraint(equalTo: view.topAnchor, constant: 13),
            rootStack.bottomAnchor.constraint(equalTo: view.bottomAnchor, constant: -10),
            factsGrid.widthAnchor.constraint(equalTo: rootStack.widthAnchor),
            lifecycleButton.widthAnchor.constraint(equalTo: rootStack.widthAnchor),
            divider.widthAnchor.constraint(equalTo: rootStack.widthAnchor),
        ])
        resizeToFitContent()
    }

    func apply(
        status: RuntimeStatus,
        audit: ProtectionAuditSnapshot = ProtectionAuditSnapshot(),
        sessionLimit: Int = RuntimeSessionCapacity.fallback
    ) {
        let presentation = RuntimePopoverPresentation.make(
            status: status,
            audit: audit,
            sessionLimit: sessionLimit
        )
        lifecycleAction = presentation.lifecycleAction
        lifecycleButton.isHidden = lifecycleAction == nil
        lifecycleButton.isEnabled = lifecycleAction != nil
        if let lifecycleAction {
            let title = actionTitle(lifecycleAction)
            lifecycleButton.title = title
            lifecycleButton.setAccessibilityLabel(title + " Agent Runtime")
        }

        for (field, fact) in zip(factValueLabels, presentation.facts) {
            field.stringValue = fact.value
            field.setAccessibilityValue(fact.value)
        }
        updateStatusIndicator(presentation.indicator, summary: presentation.accessibilitySummary)
        resizeToFitContent()
    }

    func setBusy(_ action: RuntimeAction) {
        switch action {
        case .start, .stop:
            lifecycleAction = action
        case .restart:
            lifecycleAction = nil
        }
        lifecycleButton.isHidden = lifecycleAction == nil
        lifecycleButton.isEnabled = false
        if let lifecycleAction {
            let title = actionTitle(lifecycleAction)
            lifecycleButton.title = title
            lifecycleButton.setAccessibilityLabel(title + " Agent Runtime; action in progress")
        }
        resizeToFitContent()
    }

    private func makeFactsGrid() -> NSGridView {
        let labels = ["Endpoint", "PID", "Health", "Ready", "Sessions", "Protection"]
        let rows: [[NSView]] = labels.map { label in
            let labelField = NSTextField(labelWithString: label)
            labelField.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
            labelField.textColor = .secondaryLabelColor
            labelField.setAccessibilityLabel(label)

            let valueField = NSTextField(labelWithString: "—")
            valueField.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
            valueField.textColor = .labelColor
            valueField.alignment = RuntimeFactLayout.valueAlignment
            valueField.lineBreakMode = .byTruncatingTail
            valueField.setAccessibilityLabel(label + " value")
            valueField.setAccessibilityValue(valueField.stringValue)
            factValueLabels.append(valueField)
            return [labelField, valueField]
        }
        let grid = NSGridView(views: rows)
        grid.rowSpacing = RuntimeFactLayout.rowSpacing
        grid.columnSpacing = RuntimeFactLayout.columnSpacing
        grid.column(at: RuntimeFactLayout.labelColumn).xPlacement = .leading
        grid.column(at: RuntimeFactLayout.valueColumn).xPlacement = .trailing
        grid.translatesAutoresizingMaskIntoConstraints = false
        return grid
    }

    private func configure(_ button: NSButton, title: String, action: Selector) {
        button.title = title
        button.target = self
        button.action = action
        button.bezelStyle = .rounded
        button.controlSize = .regular
        button.setAccessibilityLabel("Runtime lifecycle action")
    }

    private func actionTitle(_ action: RuntimeAction) -> String {
        switch action {
        case .start: return "Start"
        case .stop: return "Stop"
        case .restart: return ""
        }
    }

    private func updateStatusIndicator(_ indicator: RuntimeStatusIndicator, summary: String) {
        let configuration = NSImage.SymbolConfiguration(pointSize: 13, weight: .medium)
        let image = NSImage(systemSymbolName: indicator.symbolName, accessibilityDescription: indicator.accessibilityLabel)?
            .withSymbolConfiguration(configuration)
        image?.isTemplate = true
        statusIndicatorView.image = image
        statusIndicatorView.contentTintColor = indicator.color
        statusIndicatorView.toolTip = summary
        statusIndicatorView.setAccessibilityValue(summary)
    }

    private func resizeToFitContent() {
        guard isViewLoaded else { return }
        view.layoutSubtreeIfNeeded()
        let height = rootStack.fittingSize.height + 23
        guard height > 1 else { return }
        preferredContentSize = NSSize(width: 304, height: height)
        view.setFrameSize(preferredContentSize)
    }

    @objc private func lifecyclePressed() {
        guard let lifecycleAction else { return }
        performAction(lifecycleAction)
    }

    @objc private func quitPressed() { quit() }
}
