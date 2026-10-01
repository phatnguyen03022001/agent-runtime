import Darwin
import Dispatch
import Foundation

private let fileManager = FileManager.default
private let home = fileManager.homeDirectoryForCurrentUser
private let stateDirectory = home.appendingPathComponent(
    "Library/Application Support/Agent Runtime",
    isDirectory: true
)
private let runtimeEnv = stateDirectory.appendingPathComponent("runtime.env", isDirectory: false)
private let gracefulShutdownSeconds: TimeInterval = 3
private let forcedShutdownSeconds: TimeInterval = 2

private func fail(_ message: String) -> Never {
    fputs("RUNTIME SERVICE ERROR: \(message)\n", stderr)
    exit(2)
}

@MainActor
private func findTunnelClient() -> String? {
    for path in [
        "/opt/homebrew/bin/tunnel-client",
        "/usr/local/bin/tunnel-client",
        "/usr/bin/tunnel-client",
    ] where fileManager.isExecutableFile(atPath: path) {
        return path
    }
    return nil
}

@MainActor
private func spawnRuntime(
    lifecycle: String,
    tunnelClient: String,
    runtimeEnv: String
) -> (childPID: pid_t, childPGID: pid_t) {
    var attributes: posix_spawnattr_t?
    guard posix_spawnattr_init(&attributes) == 0 else {
        fail("could not initialize Runtime spawn attributes")
    }
    defer { posix_spawnattr_destroy(&attributes) }

    let flags = Int16(POSIX_SPAWN_SETPGROUP)
    guard posix_spawnattr_setflags(&attributes, flags) == 0,
          posix_spawnattr_setpgroup(&attributes, 0) == 0 else {
        fail("could not configure dedicated Runtime process group")
    }

    let arguments = ["/bin/bash", lifecycle, "--serve", tunnelClient, runtimeEnv]
    var cArguments: [UnsafeMutablePointer<CChar>?] = arguments.map { strdup($0) }
    cArguments.append(nil)
    let environment = ProcessInfo.processInfo.environment
        .map { "\($0.key)=\($0.value)" }
        .sorted()
    var cEnvironment: [UnsafeMutablePointer<CChar>?] = environment.map { strdup($0) }
    cEnvironment.append(nil)
    defer {
        for pointer in cArguments {
            if let pointer { free(pointer) }
        }
        for pointer in cEnvironment {
            if let pointer { free(pointer) }
        }
    }

    var childPID: pid_t = 0
    let result = "/bin/bash".withCString { executable in
        cArguments.withUnsafeMutableBufferPointer { argvBuffer in
            cEnvironment.withUnsafeMutableBufferPointer { envBuffer in
                posix_spawn(
                    &childPID,
                    executable,
                    nil,
                    &attributes,
                    argvBuffer.baseAddress,
                    envBuffer.baseAddress
                )
            }
        }
    }
    guard result == 0, childPID > 0 else {
        fail("could not launch installed Runtime: errno \(result)")
    }
    return (childPID, childPID)
}

private func ownedGroupAlive(_ childPGID: pid_t) -> Bool {
    errno = 0
    if kill(-childPGID, 0) == 0 {
        return true
    }
    return errno == EPERM
}

private func signalOwnedGroup(_ childPGID: pid_t, _ signalNumber: Int32) {
    if kill(-childPGID, signalNumber) != 0 && errno != ESRCH {
        fail("could not signal owned Runtime process group")
    }
}

private func waitForOwnedGroupAbsence(_ childPGID: pid_t, timeout: TimeInterval) -> Bool {
    let deadline = Date().addingTimeInterval(timeout)
    while ownedGroupAlive(childPGID) {
        if Date() >= deadline {
            return false
        }
        usleep(50_000)
    }
    return true
}

private func ensureOwnedGroupStopped(_ childPGID: pid_t, termAlreadySent: Bool) {
    guard ownedGroupAlive(childPGID) else { return }
    if !termAlreadySent {
        signalOwnedGroup(childPGID, SIGTERM)
    }
    if waitForOwnedGroupAbsence(childPGID, timeout: gracefulShutdownSeconds) {
        return
    }
    signalOwnedGroup(childPGID, SIGKILL)
    guard waitForOwnedGroupAbsence(childPGID, timeout: forcedShutdownSeconds) else {
        fail("owned Runtime process group survived bounded SIGKILL escalation")
    }
}

guard let resources = Bundle.main.resourceURL else {
    fail("app bundle resources are unavailable")
}
let runtimeRoot = resources.appendingPathComponent("runtime", isDirectory: true)
let lifecycle = runtimeRoot.appendingPathComponent("start.sh", isDirectory: false)
guard fileManager.isExecutableFile(atPath: lifecycle.path) else {
    fail("installed Runtime lifecycle helper is unavailable")
}
guard let tunnelClient = findTunnelClient() else {
    fail("tunnel-client is unavailable")
}
guard fileManager.isReadableFile(atPath: runtimeEnv.path) else {
    fail("canonical runtime.env is unavailable")
}

let spawned = spawnRuntime(
    lifecycle: lifecycle.path,
    tunnelClient: tunnelClient,
    runtimeEnv: runtimeEnv.path
)
let childPID = spawned.childPID
let childPGID = spawned.childPGID

signal(SIGTERM, SIG_IGN)
signal(SIGINT, SIG_IGN)
let shutdownRequest = DispatchSemaphore(value: 0)
let signalQueue = DispatchQueue(label: "com.picmao.agent-runtime.runtime-service.signals")
let termSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: signalQueue)
let intSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: signalQueue)
termSource.setEventHandler { shutdownRequest.signal() }
intSource.setEventHandler { shutdownRequest.signal() }
termSource.resume()
intSource.resume()

var childStatus: Int32 = 0
var childReaped = false
var termSent = false
var termDeadline: Date?
var killSent = false

while !childReaped {
    let waitResult = waitpid(childPID, &childStatus, WNOHANG)
    if waitResult == childPID {
        childReaped = true
        break
    }
    if waitResult == -1 {
        if errno == EINTR {
            continue
        }
        fail("waitpid failed for exact Runtime child")
    }

    if !termSent && shutdownRequest.wait(timeout: .now()) == .success {
        signalOwnedGroup(childPGID, SIGTERM)
        termSent = true
        termDeadline = Date().addingTimeInterval(gracefulShutdownSeconds)
    }
    if termSent,
       !killSent,
       let deadline = termDeadline,
       Date() >= deadline,
       ownedGroupAlive(childPGID) {
        signalOwnedGroup(childPGID, SIGKILL)
        killSent = true
    }
    usleep(50_000)
}

ensureOwnedGroupStopped(childPGID, termAlreadySent: termSent)
termSource.cancel()
intSource.cancel()

// This helper owns exactly one explicitly launched Runtime generation.
// launchd KeepAlive is disabled, so cleanup completes this invocation without
// requesting automatic recovery after child failure or operator Stop.
exit(0)
