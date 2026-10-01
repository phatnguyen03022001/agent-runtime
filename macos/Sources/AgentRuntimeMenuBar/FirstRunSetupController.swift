import AppKit
import Foundation

struct FirstRunHelpLinks {
    static let apiKeys = URL(string: "https://platform.openai.com/settings/organization/api-keys")!
    static let tunnels = URL(string: "https://platform.openai.com/settings/organization/tunnels")!
    static let secureTunnel = URL(string: "https://developers.openai.com/api/docs/guides/secure-mcp-tunnels")!
}

enum FirstRunFailurePresentation {
    static func message(from data: Data, fallback: String) -> String {
        guard let reason = reasonCode(from: data) else { return fallback }
        switch reason {
        case "TUNNEL_CLIENT_UNAVAILABLE":
            return "The official tunnel-client is unavailable. Install it using the Secure MCP Tunnel setup guide and try again."
        case "INVALID_TUNNEL_ID":
            return "Enter a valid Tunnel ID from your OpenAI organization."
        case "INVALID_CREDENTIAL":
            return "The Runtime API key was rejected. Create or check the API key and try again."
        case "TUNNEL_ACCESS_DENIED":
            return "The Runtime API key does not have access to that tunnel."
        case "TUNNEL_NOT_FOUND":
            return "The configured tunnel was not found. Check the Tunnel ID and try again."
        case "CONTROL_PLANE_UNAVAILABLE":
            return "The OpenAI control plane is temporarily unavailable. Try again later."
        case "WORKSPACE_UNAVAILABLE", "WORKSPACE_INVALID":
            return "Choose an existing workspace folder and try again."
        case "RUNTIME_CONFIGURATION_INCOMPLETE":
            return "Provide all required Runtime configuration fields and try again."
        case "CUTOVER_TRANSACTION_PRESENT":
            return "An existing cutover transaction must be resolved before setup can continue."
        default:
            return fallback
        }
    }

    static func reasonCode(from data: Data) -> String? {
        guard let value = try? JSONSerialization.jsonObject(with: data),
              let object = value as? [String: Any] else {
            return nil
        }
        if let reason = object["reason_code"] as? String, !reason.isEmpty {
            return reason
        }
        guard let checks = object["checks"] as? [[String: Any]] else { return nil }
        for check in checks where (check["status"] as? String) != "pass" {
            if let reason = check["reason_code"] as? String, !reason.isEmpty {
                return reason
            }
        }
        return nil
    }
}

struct FirstRunProcessResult: Equatable {
    let exitCode: Int32
    let standardOutput: Data
    let standardError: Data

    init(
        exitCode: Int32,
        standardOutput: Data = Data(),
        standardError: Data = Data()
    ) {
        self.exitCode = exitCode
        self.standardOutput = standardOutput
        self.standardError = standardError
    }
}

protocol FirstRunProcessRunning: AnyObject {
    func run(executable: URL, arguments: [String], standardInput: Data?) -> FirstRunProcessResult
}

final class FoundationFirstRunProcessRunner: FirstRunProcessRunning {
    func run(executable: URL, arguments: [String], standardInput: Data?) -> FirstRunProcessResult {
        let process = Process()
        let output = Pipe()
        let error = Pipe()
        process.executableURL = executable
        process.arguments = arguments
        process.environment = Self.safeEnvironment()
        process.standardOutput = output
        process.standardError = error

        var input: Pipe?
        if standardInput != nil {
            let pipe = Pipe()
            process.standardInput = pipe
            input = pipe
        } else {
            process.standardInput = FileHandle.nullDevice
        }

        do {
            try process.run()
            if let standardInput, let input {
                input.fileHandleForWriting.write(standardInput)
                try? input.fileHandleForWriting.close()
            }
            process.waitUntilExit()
        } catch {
            try? input?.fileHandleForWriting.close()
            return FirstRunProcessResult(
                exitCode: 127,
                standardError: Data("process launch failed".utf8)
            )
        }

        return FirstRunProcessResult(
            exitCode: process.terminationStatus,
            standardOutput: output.fileHandleForReading.readDataToEndOfFile(),
            standardError: error.fileHandleForReading.readDataToEndOfFile()
        )
    }

    private static func safeEnvironment() -> [String: String] {
        let source = ProcessInfo.processInfo.environment
        var result: [String: String] = [:]
        for key in ["HOME", "USER", "TMPDIR", "LANG"] {
            if let value = source[key], !value.isEmpty {
                result[key] = value
            }
        }
        for (key, value) in source where key.hasPrefix("LC_") && !value.isEmpty {
            result[key] = value
        }
        var path = (source["PATH"] ?? "").split(separator: ":").map(String.init)
        for candidate in ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
            where !path.contains(candidate) {
            path.append(candidate)
        }
        result["PATH"] = path.joined(separator: ":")
        return result
    }
}

struct FirstRunSetupPaths: Equatable {
    let candidateApp: URL
    let home: URL
    let runtimePython: URL
    let runtimeConfig: URL
    let installer: URL
    let canonicalEnv: URL
    let handoff: URL
    let installedApp: URL
    let installedStart: URL
    let transactionDirectory: URL

    init(candidateApp: URL, home: URL) {
        self.candidateApp = candidateApp.standardizedFileURL
        self.home = home.standardizedFileURL
        let runtime = candidateApp
            .appendingPathComponent("Contents/Resources/runtime", isDirectory: true)
        runtimePython = runtime.appendingPathComponent(".venv/bin/python", isDirectory: false)
        runtimeConfig = runtime.appendingPathComponent("macos/runtime_config.py", isDirectory: false)
        installer = runtime.appendingPathComponent("macos/install_release.sh", isDirectory: false)
        canonicalEnv = home.appendingPathComponent(
            "Library/Application Support/Agent Runtime/runtime.env",
            isDirectory: false
        )
        handoff = candidateApp.deletingLastPathComponent()
            .appendingPathComponent("Agent Runtime.candidate.json", isDirectory: false)
        installedApp = home.appendingPathComponent("Applications/Agent Runtime.app", isDirectory: true)
        installedStart = installedApp.appendingPathComponent(
            "Contents/Resources/runtime/start.sh",
            isDirectory: false
        )
        transactionDirectory = home.appendingPathComponent(
            "Library/Application Support/Agent Runtime/cutover-transaction",
            isDirectory: true
        )
    }

    static func production() -> FirstRunSetupPaths {
        FirstRunSetupPaths(
            candidateApp: Bundle.main.bundleURL,
            home: FileManager.default.homeDirectoryForCurrentUser
        )
    }
}

struct FirstRunSetupInput {
    let apiKey: String
    let tunnelID: String
    let workspace: URL
    let gitName: String
    let gitEmail: String
}

enum FirstRunConfigurationState: Equatable {
    case missing
    case valid(workspace: URL)
    case invalid(String)
}

enum FirstRunLaunchDecision: Equatable {
    case showFresh
    case showConfigured(URL)
    case showBlocked(String)
    case controlPanel
}

enum FirstRunLaunchPolicy {
    static func decide(
        configuration: FirstRunConfigurationState,
        activationRequired: Bool
    ) -> FirstRunLaunchDecision {
        switch configuration {
        case .missing:
            return .showFresh
        case .valid(let workspace):
            return activationRequired ? .showConfigured(workspace) : .controlPanel
        case .invalid(let message):
            return .showBlocked(message)
        }
    }
}

enum FirstRunRecoveryAction: Equatable {
    case recheck
    case resume
    case rollback
    case recover
}

enum FirstRunSetupOutcome: Equatable {
    case success
    case actionRequired(String, FirstRunRecoveryAction)
    case failure(String)
}

enum FirstRunGateDecision: Equatable {
    case commitAllowed
    case recoverPartial
    case blocked
}

enum FirstRunDoctorGate {
    static func preCommitDecision(from data: Data) -> FirstRunGateDecision {
        guard let report = object(from: data),
              let status = report["status"] as? String,
              let checks = report["checks"] as? [[String: Any]],
              let cutover = checks.first(where: { ($0["id"] as? String) == "cutover_identity" }),
              let reason = cutover["reason_code"] as? String,
              reason == "CUTOVER_TRANSACTION_PRESENT",
              let evidence = cutover["evidence"] as? [String: Any],
              evidence["transaction_present"] as? Bool == true,
              evidence["phase"] as? String == "APP_SWAPPED",
              let transactionStatus = evidence["status"] as? String else {
            return .blocked
        }

        let recognizedStatuses = Set(["pass", "warn", "fail"])
        guard checks.allSatisfy({
            guard let checkStatus = $0["status"] as? String else { return false }
            return recognizedStatuses.contains(checkStatus)
        }) else {
            return .blocked
        }

        let failures = checks.filter { ($0["status"] as? String) == "fail" }
        let warnings = checks.filter { ($0["status"] as? String) == "warn" }

        if transactionStatus == "PARTIAL" {
            return .recoverPartial
        }

        guard transactionStatus == "PENDING",
              status == "degraded",
              failures.isEmpty,
              warnings.count == 1,
              (warnings[0]["id"] as? String) == "cutover_identity",
              (warnings[0]["reason_code"] as? String) == "CUTOVER_TRANSACTION_PRESENT" else {
            return .blocked
        }
        return .commitAllowed
    }

    static func postCommitIsHealthy(from data: Data) -> Bool {
        guard let report = object(from: data),
              report["status"] as? String == "healthy",
              let checks = report["checks"] as? [[String: Any]],
              checks.allSatisfy({ ($0["status"] as? String) == "pass" }),
              let cutover = checks.first(where: { ($0["id"] as? String) == "cutover_identity" }),
              cutover["status"] as? String == "pass",
              cutover["reason_code"] as? String == "OK",
              let evidence = cutover["evidence"] as? [String: Any],
              evidence["transaction_present"] as? Bool == false else {
            return false
        }
        return true
    }

    private static func object(from data: Data) -> [String: Any]? {
        guard let value = try? JSONSerialization.jsonObject(with: data),
              let object = value as? [String: Any] else {
            return nil
        }
        return object
    }
}

final class FirstRunSetupOrchestrator {
    let paths: FirstRunSetupPaths
    private let runner: FirstRunProcessRunning
    private let fileManager: FileManager

    init(
        paths: FirstRunSetupPaths,
        runner: FirstRunProcessRunning = FoundationFirstRunProcessRunner(),
        fileManager: FileManager = .default
    ) {
        self.paths = paths
        self.runner = runner
        self.fileManager = fileManager
    }

    func inspectConfiguration() -> FirstRunConfigurationState {
        guard pathExistsOrIsSymlink(paths.canonicalEnv) else {
            return .missing
        }
        let result = runner.run(
            executable: paths.runtimePython,
            arguments: [
                paths.runtimeConfig.path,
                "--inspect-prebuilt-existing",
                paths.canonicalEnv.path,
            ],
            standardInput: nil
        )
        guard result.exitCode == 0,
              let object = try? JSONSerialization.jsonObject(with: result.standardOutput) as? [String: Any],
              let workspace = object["workspace_root"] as? String,
              workspace.hasPrefix("/") else {
            return .invalid(
                "Existing canonical runtime.env is invalid or unsafe. Setup will not overwrite it."
            )
        }
        return .valid(workspace: URL(fileURLWithPath: workspace, isDirectory: true))
    }

    func requiresOnboardingForValidConfiguration() -> Bool {
        if pathExistsOrIsSymlink(paths.transactionDirectory) {
            return true
        }
        return paths.candidateApp.standardizedFileURL.path != paths.installedApp.standardizedFileURL.path
    }

    func begin(_ input: FirstRunSetupInput) -> FirstRunSetupOutcome {
        guard safeReleaseBundleIsPresent() else {
            return .failure(
                "The complete release bundle is required. Reopen Agent Runtime.app beside Agent Runtime.candidate.json."
            )
        }
        guard input.workspace.path.hasPrefix("/"), isExistingDirectory(input.workspace) else {
            return .failure("Choose an existing workspace folder.")
        }
        guard let encoded = validatedInput(input) else {
            return .failure("Provide all required Runtime configuration fields.")
        }

        let configured = runner.run(
            executable: paths.runtimePython,
            arguments: [
                paths.runtimeConfig.path,
                "--prebuilt-stdin",
                paths.canonicalEnv.path,
            ],
            standardInput: encoded
        )
        guard configured.exitCode == 0 else {
            return .failure(FirstRunFailurePresentation.message(
                from: configured.standardOutput,
                fallback: "Runtime configuration was rejected. Check the required values and try again."
            ))
        }
        return activateConfigured(workspace: input.workspace)
    }

    func reconfigure(_ input: FirstRunSetupInput) -> FirstRunSetupOutcome {
        guard let encoded = validatedInput(input) else {
            return .failure("Provide all required Runtime configuration fields and choose an existing workspace.")
        }
        let configured = runner.run(
            executable: paths.runtimePython,
            arguments: [
                paths.runtimeConfig.path,
                "--reconfigure-stdin",
                paths.canonicalEnv.path,
            ],
            standardInput: encoded
        )
        guard configured.exitCode == 0 else {
            return .failure(FirstRunFailurePresentation.message(
                from: configured.standardOutput,
                fallback: "Runtime reconfiguration was rejected. Check the required values and try again."
            ))
        }
        return .success
    }

    private func validatedInput(_ input: FirstRunSetupInput) -> Data? {
        let required = [input.apiKey, input.tunnelID, input.gitName, input.gitEmail]
        guard required.allSatisfy({ !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }),
              input.workspace.path.hasPrefix("/"),
              isExistingDirectory(input.workspace) else {
            return nil
        }
        let payload: [String: String] = [
            "CONTROL_PLANE_API_KEY": input.apiKey,
            "CONTROL_PLANE_TUNNEL_ID": input.tunnelID,
            "AGENT_RUNTIME_WORKSPACE_ROOT": input.workspace.standardizedFileURL.path,
            "AGENT_RUNTIME_GIT_NAME": input.gitName,
            "AGENT_RUNTIME_GIT_EMAIL": input.gitEmail,
        ]
        return try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
    }

    func continueConfigured(workspace: URL) -> FirstRunSetupOutcome {
        if pathExistsOrIsSymlink(paths.transactionDirectory) {
            return evaluateAndFinalize()
        }
        return activateConfigured(workspace: workspace)
    }

    func activateConfigured(workspace: URL) -> FirstRunSetupOutcome {
        guard safeReleaseBundleIsPresent() else {
            return .failure(
                "The complete release bundle is required. Reopen Agent Runtime.app beside Agent Runtime.candidate.json."
            )
        }
        guard workspace.path.hasPrefix("/"), isExistingDirectory(workspace) else {
            return .failure("The configured workspace is unavailable.")
        }
        let result = runInstaller(["--workspace-root", workspace.standardizedFileURL.path])
        if result.exitCode != 0 && !pathExistsOrIsSymlink(paths.transactionDirectory) {
            return .failure(FirstRunFailurePresentation.message(
                from: result.standardOutput,
                fallback: "Prebuilt activation failed. See installation guidance for the bounded next action."
            ))
        }
        return evaluateAndFinalize()
    }

    func perform(_ action: FirstRunRecoveryAction) -> FirstRunSetupOutcome {
        switch action {
        case .recheck:
            return evaluateAndFinalize()
        case .resume:
            let result = runInstaller(["--resume-cutover"])
            if result.exitCode != 0 && !pathExistsOrIsSymlink(paths.transactionDirectory) {
                return .failure("Cutover resume failed. " + boundedDetail(result.standardError))
            }
            return evaluateAndFinalize()
        case .rollback:
            let result = runInstaller(["--rollback-cutover"])
            guard result.exitCode == 0 else {
                return .failure("Cutover rollback failed. " + boundedDetail(result.standardError))
            }
            return .failure("Cutover was rolled back safely. Reopen the complete release bundle to try setup again.")
        case .recover:
            let result = runInstaller(["--recover-partial-cutover"])
            guard result.exitCode == 0 else {
                return .failure("Partial cutover recovery failed. " + boundedDetail(result.standardError))
            }
            return .failure("Partial cutover recovery completed. Reopen the complete release bundle to continue.")
        }
    }

    private func evaluateAndFinalize() -> FirstRunSetupOutcome {
        let doctor = runDoctor()
        switch FirstRunDoctorGate.preCommitDecision(from: doctor.standardOutput) {
        case .recoverPartial:
            return .actionRequired(
                "A partial cutover was detected. Use the existing recovery path before continuing.",
                .recover
            )
        case .blocked:
            if pathExistsOrIsSymlink(paths.transactionDirectory) {
                return .actionRequired(
                    "Setup has not reached the safe commit gate. No commit was attempted.",
                    .rollback
                )
            }
            return .failure(
                "Setup state is not safe to finalize. " + boundedDetail(doctor.standardError)
            )
        case .commitAllowed:
            break
        }

        let committed = runInstaller(["--commit-cutover"])
        guard committed.exitCode == 0 else {
            return .actionRequired(
                "Cutover commit did not complete. State will be rechecked before any retry.",
                .recheck
            )
        }

        let postDoctor = runDoctor()
        guard !pathExistsOrIsSymlink(paths.transactionDirectory),
              FirstRunDoctorGate.postCommitIsHealthy(from: postDoctor.standardOutput) else {
            return .failure(
                "Cutover committed, but installed Runtime state is not healthy. Use the installed Agent Runtime recovery guidance."
            )
        }
        return .success
    }

    private func runInstaller(_ arguments: [String]) -> FirstRunProcessResult {
        runner.run(
            executable: URL(fileURLWithPath: "/bin/bash"),
            arguments: [paths.installer.path] + arguments,
            standardInput: nil
        )
    }

    private func runDoctor() -> FirstRunProcessResult {
        runner.run(
            executable: URL(fileURLWithPath: "/bin/bash"),
            arguments: [paths.installedStart.path, "doctor", "--json"],
            standardInput: nil
        )
    }

    private func safeReleaseBundleIsPresent() -> Bool {
        isSafeDirectory(paths.candidateApp) && isSafeRegularFile(paths.handoff)
    }

    private func isExistingDirectory(_ url: URL) -> Bool {
        var isDirectory: ObjCBool = false
        return fileManager.fileExists(atPath: url.path, isDirectory: &isDirectory) && isDirectory.boolValue
    }

    private func isSafeDirectory(_ url: URL) -> Bool {
        guard isExistingDirectory(url),
              let values = try? url.resourceValues(forKeys: [.isSymbolicLinkKey, .isDirectoryKey]) else {
            return false
        }
        return values.isSymbolicLink != true && values.isDirectory == true
    }

    private func isSafeRegularFile(_ url: URL) -> Bool {
        guard fileManager.fileExists(atPath: url.path),
              let values = try? url.resourceValues(forKeys: [.isSymbolicLinkKey, .isRegularFileKey]) else {
            return false
        }
        return values.isSymbolicLink != true && values.isRegularFile == true
    }

    private func pathExistsOrIsSymlink(_ url: URL) -> Bool {
        if fileManager.fileExists(atPath: url.path) {
            return true
        }
        return (try? fileManager.destinationOfSymbolicLink(atPath: url.path)) != nil
    }

    private func boundedDetail(_ data: Data, secrets: [String] = []) -> String {
        var detail = String(decoding: data.prefix(1024), as: UTF8.self)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        for secret in secrets where !secret.isEmpty {
            detail = detail.replacingOccurrences(of: secret, with: "[redacted]")
        }
        return detail.isEmpty ? "See recovery guidance for the bounded next action." : detail
    }
}

enum FirstRunSetupMode {
    case fresh
    case configured(URL)
    case reconfigure(URL?)
    case blocked(String)
}

@MainActor
final class FirstRunSetupController: NSViewController, NSTextFieldDelegate {
    let apiKeyField = NSSecureTextField()
    let tunnelIDField = NSSecureTextField()
    let workspaceField = NSTextField(labelWithString: "No workspace selected")
    let gitNameField = NSTextField()
    let gitEmailField = NSTextField()
    var selectedWorkspace: URL?

    private let statusField = NSTextField(wrappingLabelWithString: "")
    private let primaryButton = NSButton()
    private let secondaryButton = NSButton()
    private let mode: FirstRunSetupMode
    private let begin: (FirstRunSetupInput) -> FirstRunSetupOutcome
    private let reconfigure: (FirstRunSetupInput) -> FirstRunSetupOutcome
    private let activateConfigured: (URL) -> FirstRunSetupOutcome
    private let performAction: (FirstRunRecoveryAction) -> FirstRunSetupOutcome
    private let requestReconfigure: (URL) -> Void
    private let completed: () -> Void
    private var recoveryAction: FirstRunRecoveryAction?

    init(
        mode: FirstRunSetupMode,
        begin: @escaping (FirstRunSetupInput) -> FirstRunSetupOutcome,
        reconfigure: @escaping (FirstRunSetupInput) -> FirstRunSetupOutcome = { _ in
            .failure("Reconfiguration is unavailable.")
        },
        activateConfigured: @escaping (URL) -> FirstRunSetupOutcome,
        performAction: @escaping (FirstRunRecoveryAction) -> FirstRunSetupOutcome,
        requestReconfigure: @escaping (URL) -> Void = { _ in },
        completed: @escaping () -> Void
    ) {
        self.mode = mode
        self.begin = begin
        self.reconfigure = reconfigure
        self.activateConfigured = activateConfigured
        self.performAction = performAction
        self.requestReconfigure = requestReconfigure
        self.completed = completed
        super.init(nibName: nil, bundle: nil)
        preferredContentSize = NSSize(width: 500, height: 420)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func loadView() {
        let root = NSView()
        let titleText: String
        if case .reconfigure = mode {
            titleText = "Reconfigure Agent Runtime"
        } else {
            titleText = "Set Up Agent Runtime"
        }
        let title = NSTextField(labelWithString: titleText)
        title.font = .systemFont(ofSize: 20, weight: .semibold)

        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 10
        stack.translatesAutoresizingMaskIntoConstraints = false
        stack.addArrangedSubview(title)

        switch mode {
        case .fresh:
            addConfigurationForm(to: stack, workspace: nil)
            primaryButton.title = "Set Up"
        case .configured(let workspace):
            selectedWorkspace = workspace
            workspaceField.stringValue = workspace.path
            stack.addArrangedSubview(NSTextField(wrappingLabelWithString:
                "A valid canonical Runtime configuration already exists. Continue activation without rewriting it, or explicitly reconfigure it."
            ))
            stack.addArrangedSubview(workspaceField)
            let reconfigureButton = NSButton(
                title: "Reconfigure…",
                target: self,
                action: #selector(requestReconfigurePressed)
            )
            stack.addArrangedSubview(reconfigureButton)
            primaryButton.title = "Continue Setup"
        case .reconfigure(let workspace):
            if let workspace {
                selectedWorkspace = workspace.standardizedFileURL
                workspaceField.stringValue = workspace.standardizedFileURL.path
            }
            stack.addArrangedSubview(NSTextField(wrappingLabelWithString:
                "Enter new values. Existing API keys and Tunnel IDs are never displayed or prefilled."
            ))
            addConfigurationForm(to: stack, workspace: workspace)
            primaryButton.title = "Reconfigure"
        case .blocked(let message):
            statusField.stringValue = message
            primaryButton.isHidden = true
            primaryButton.isEnabled = false
        }

        statusField.textColor = .secondaryLabelColor
        statusField.maximumNumberOfLines = 5
        stack.addArrangedSubview(statusField)

        primaryButton.target = self
        primaryButton.action = #selector(primaryPressed)
        primaryButton.bezelStyle = .rounded
        stack.addArrangedSubview(primaryButton)

        secondaryButton.target = self
        secondaryButton.action = #selector(secondaryPressed)
        secondaryButton.bezelStyle = .rounded
        secondaryButton.isHidden = true
        stack.addArrangedSubview(secondaryButton)

        root.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 24),
            stack.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -24),
            stack.topAnchor.constraint(equalTo: root.topAnchor, constant: 24),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: root.bottomAnchor, constant: -24),
            primaryButton.widthAnchor.constraint(equalTo: stack.widthAnchor),
            secondaryButton.widthAnchor.constraint(equalTo: stack.widthAnchor),
        ])
        view = root

        switch mode {
        case .fresh, .reconfigure:
            refreshRequiredFieldState()
        case .configured, .blocked:
            break
        }
    }

    private func addConfigurationForm(to stack: NSStackView, workspace: URL?) {
        apiKeyField.stringValue = ""
        tunnelIDField.stringValue = ""
        apiKeyField.delegate = self
        tunnelIDField.delegate = self
        gitNameField.delegate = self
        gitEmailField.delegate = self

        stack.addArrangedSubview(fieldRow("Runtime API key", apiKeyField))
        stack.addArrangedSubview(fieldRow("Tunnel ID", tunnelIDField))
        stack.addArrangedSubview(fieldRow("Git name", gitNameField))
        stack.addArrangedSubview(fieldRow("Git email", gitEmailField))

        let choose = NSButton(title: "Choose Workspace…", target: self, action: #selector(chooseWorkspace))
        let workspaceRow = NSStackView(views: [workspaceField, choose])
        workspaceRow.orientation = .horizontal
        workspaceRow.spacing = 8
        workspaceRow.distribution = .fill
        stack.addArrangedSubview(workspaceRow)
        workspaceRow.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true

        let links = NSStackView(views: [
            helpButton("Runtime API keys…", action: #selector(openAPIKeysHelp)),
            helpButton("Tunnels…", action: #selector(openTunnelsHelp)),
            helpButton("Secure MCP Tunnel help…", action: #selector(openSecureTunnelHelp)),
        ])
        links.orientation = .horizontal
        links.spacing = 10
        stack.addArrangedSubview(links)
    }

    private func helpButton(_ title: String, action: Selector) -> NSButton {
        let button = NSButton(title: title, target: self, action: action)
        button.isBordered = false
        button.bezelStyle = .inline
        button.controlSize = .small
        return button
    }

    static func configureWorkspacePanel(_ panel: NSOpenPanel) {
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = false
        panel.resolvesAliases = true
        panel.prompt = "Choose Workspace"
    }

    func applyWorkspaceSelection(response: NSApplication.ModalResponse, urls: [URL]) {
        guard response == .OK, urls.count == 1 else { return }
        selectedWorkspace = urls[0].standardizedFileURL
        workspaceField.stringValue = urls[0].standardizedFileURL.path
        refreshRequiredFieldState()
    }

    func controlTextDidChange(_ obj: Notification) {
        refreshRequiredFieldState()
    }

    func refreshRequiredFieldState(updateFeedback: Bool = true) {
        guard case .fresh = mode else {
            if case .reconfigure = mode {
                updateRequiredFieldState(updateFeedback: updateFeedback)
            }
            return
        }
        updateRequiredFieldState(updateFeedback: updateFeedback)
    }

    private func updateRequiredFieldState(updateFeedback: Bool) {
        let fields = [
            apiKeyField.stringValue,
            tunnelIDField.stringValue,
            gitNameField.stringValue,
            gitEmailField.stringValue,
        ]
        var isDirectory: ObjCBool = false
        let workspaceReady = selectedWorkspace.map {
            FileManager.default.fileExists(atPath: $0.path, isDirectory: &isDirectory) && isDirectory.boolValue
        } ?? false
        let ready = fields.allSatisfy {
            !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        } && workspaceReady
        primaryButton.isEnabled = ready
        if updateFeedback {
            statusField.stringValue = ready
                ? "Ready to validate configuration."
                : "Required: Runtime API key, Tunnel ID, Git name, Git email, and an existing workspace."
        }
    }

    private func fieldRow(_ label: String, _ field: NSTextField) -> NSView {
        let labelField = NSTextField(labelWithString: label)
        labelField.frame.size.width = 150
        field.placeholderString = label
        let row = NSStackView(views: [labelField, field])
        row.orientation = .horizontal
        row.spacing = 10
        row.distribution = .fill
        field.widthAnchor.constraint(greaterThanOrEqualToConstant: 250).isActive = true
        return row
    }

    @objc private func chooseWorkspace() {
        let panel = NSOpenPanel()
        Self.configureWorkspacePanel(panel)
        let response = panel.runModal()
        applyWorkspaceSelection(response: response, urls: panel.urls)
    }

    @objc private func requestReconfigurePressed() {
        guard case .configured(let workspace) = mode else { return }
        requestReconfigure(workspace)
    }

    @objc private func openAPIKeysHelp() {
        NSWorkspace.shared.open(FirstRunHelpLinks.apiKeys)
    }

    @objc private func openTunnelsHelp() {
        NSWorkspace.shared.open(FirstRunHelpLinks.tunnels)
    }

    @objc private func openSecureTunnelHelp() {
        NSWorkspace.shared.open(FirstRunHelpLinks.secureTunnel)
    }

    @objc private func primaryPressed() {
        primaryButton.isEnabled = false
        secondaryButton.isHidden = true
        recoveryAction = nil

        let outcome: FirstRunSetupOutcome
        switch mode {
        case .fresh, .reconfigure:
            guard let input = currentInput() else {
                statusField.stringValue = "Provide all required fields and choose an existing workspace."
                refreshRequiredFieldState(updateFeedback: false)
                return
            }
            if case .fresh = mode {
                statusField.stringValue = "Validating configuration before setup…"
                outcome = begin(input)
            } else {
                statusField.stringValue = "Validating new configuration…"
                outcome = reconfigure(input)
            }
            apiKeyField.stringValue = ""
            tunnelIDField.stringValue = ""
        case .configured(let workspace):
            statusField.stringValue = "Checking activation…"
            outcome = activateConfigured(workspace)
        case .blocked:
            return
        }
        apply(outcome)
    }

    private func currentInput() -> FirstRunSetupInput? {
        guard let workspace = selectedWorkspace else { return nil }
        let values = [
            apiKeyField.stringValue,
            tunnelIDField.stringValue,
            gitNameField.stringValue,
            gitEmailField.stringValue,
        ]
        guard values.allSatisfy({ !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }) else {
            return nil
        }
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: workspace.path, isDirectory: &isDirectory),
              isDirectory.boolValue else {
            return nil
        }
        return FirstRunSetupInput(
            apiKey: apiKeyField.stringValue,
            tunnelID: tunnelIDField.stringValue,
            workspace: workspace,
            gitName: gitNameField.stringValue,
            gitEmail: gitEmailField.stringValue
        )
    }

    @objc private func secondaryPressed() {
        guard let recoveryAction else { return }
        secondaryButton.isEnabled = false
        apply(performAction(recoveryAction))
    }

    private func apply(_ outcome: FirstRunSetupOutcome) {
        switch outcome {
        case .success:
            if case .reconfigure = mode {
                statusField.stringValue =
                    "Configuration updated. Runtime was not stopped or restarted; use an explicit lifecycle action when needed."
            } else {
                statusField.stringValue = "Setup complete. Runtime is installed and remains stopped until Start."
            }
            primaryButton.isHidden = true
            secondaryButton.isHidden = true
            completed()
        case .actionRequired(let message, let action):
            statusField.stringValue = message
            let title: String
            switch action {
            case .recheck: title = "Check Again"
            case .resume: title = "Continue"
            case .rollback: title = "Roll Back"
            case .recover: title = "Recover"
            }
            showSecondary(title: title, action: action)
        case .failure(let message):
            statusField.stringValue = message
            secondaryButton.isHidden = true
            switch mode {
            case .fresh, .reconfigure:
                refreshRequiredFieldState(updateFeedback: false)
            case .configured:
                primaryButton.isEnabled = true
            case .blocked:
                primaryButton.isEnabled = false
            }
        }
    }

    private func showSecondary(title: String, action: FirstRunRecoveryAction) {
        recoveryAction = action
        secondaryButton.title = title
        secondaryButton.isHidden = false
        secondaryButton.isEnabled = true
        primaryButton.isEnabled = false
    }
}
