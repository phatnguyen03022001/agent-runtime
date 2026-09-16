import Darwin
import Dispatch
import Foundation

private let fileManager = FileManager.default
private let home = fileManager.homeDirectoryForCurrentUser
private let stateDirectory = home.appendingPathComponent(
    "Library/Application Support/Agent Runtime",
    isDirectory: true
)
private let desiredState = stateDirectory.appendingPathComponent(
    "protected-runtime-running",
    isDirectory: false
)
private let runtimeEnv = stateDirectory.appendingPathComponent("runtime.env", isDirectory: false)

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

guard fileManager.fileExists(atPath: desiredState.path) else {
    exit(0)
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

let child = Process()
child.executableURL = URL(fileURLWithPath: "/bin/bash")
child.arguments = [lifecycle.path, "--serve", tunnelClient, runtimeEnv.path]
child.standardOutput = FileHandle.standardOutput
child.standardError = FileHandle.standardError

signal(SIGTERM, SIG_IGN)
signal(SIGINT, SIG_IGN)
let termSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .global(qos: .userInitiated))
let intSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .global(qos: .userInitiated))
termSource.setEventHandler { if child.isRunning { child.terminate() } }
intSource.setEventHandler { if child.isRunning { child.interrupt() } }
termSource.resume()
intSource.resume()

do {
    try child.run()
} catch {
    fail("could not launch installed Runtime: \(error.localizedDescription)")
}
child.waitUntilExit()
termSource.cancel()
intSource.cancel()

// KeepAlive uses SuccessfulExit=false. While desired state stays RUNNING,
// any child exit becomes non-zero so launchd recovers it. Explicit Stop
// removes the marker first and signals this service, which then exits zero.
exit(fileManager.fileExists(atPath: desiredState.path) ? 1 : 0)
