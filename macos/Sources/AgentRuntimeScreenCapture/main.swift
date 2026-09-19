import AgentRuntimeCore
import CoreGraphics
import Darwin
import Foundation

private enum CLIError: Error {
    case invalid(String)
}

private func parseArguments(_ arguments: [String]) throws -> ScreenCaptureRequest {
    var values: [String: String] = [:]
    var index = 0
    while index < arguments.count {
        let key = arguments[index]
        guard key.hasPrefix("--"), index + 1 < arguments.count else {
            throw CLIError.invalid("Every screen capture flag requires one value.")
        }
        guard values[key] == nil else {
            throw CLIError.invalid("Duplicate screen capture flag.")
        }
        let allowed = Set([
            "--target", "--window-id", "--application-bundle-id", "--display-id",
            "--x", "--y", "--width", "--height",
        ])
        guard allowed.contains(key) else {
            throw CLIError.invalid("Unknown screen capture flag.")
        }
        values[key] = arguments[index + 1]
        index += 2
    }

    guard let rawTarget = values["--target"],
          let target = ScreenCaptureTarget(rawValue: rawTarget) else {
        throw CLIError.invalid("A valid --target is required.")
    }
    let windowID = try parseUInt32(values["--window-id"], name: "window_id")
    let displayID = try parseUInt32(values["--display-id"], name: "display_id")
    let bundleID = values["--application-bundle-id"]
    let x = try parseDouble(values["--x"], name: "x")
    let y = try parseDouble(values["--y"], name: "y")
    let width = try parseDouble(values["--width"], name: "width")
    let height = try parseDouble(values["--height"], name: "height")
    let geometry = [x, y, width, height]
    let region: ScreenCaptureRect?
    if geometry.allSatisfy({ $0 != nil }) {
        guard let x, let y, let width, let height, width > 0, height > 0 else {
            throw CLIError.invalid("Region geometry must be finite and positive.")
        }
        region = ScreenCaptureRect(x: x, y: y, width: width, height: height)
    } else if geometry.allSatisfy({ $0 == nil }) {
        region = nil
    } else {
        throw CLIError.invalid("Region geometry requires x, y, width, and height together.")
    }
    return ScreenCaptureRequest(
        target: target,
        windowID: windowID,
        applicationBundleID: bundleID,
        displayID: displayID,
        region: region
    )
}

private func parseUInt32(_ value: String?, name: String) throws -> UInt32? {
    guard let value else { return nil }
    guard let parsed = UInt32(value), parsed > 0 else {
        throw CLIError.invalid("\(name) must be a positive 32-bit integer.")
    }
    return parsed
}

private func parseDouble(_ value: String?, name: String) throws -> Double? {
    guard let value else { return nil }
    guard let parsed = Double(value), parsed.isFinite else {
        throw CLIError.invalid("\(name) must be finite.")
    }
    return parsed
}

private func writeSuccess(_ result: ScreenCaptureResult) throws {
    FileHandle.standardOutput.write(
        try ScreenCaptureWire.success(metadata: result.metadata, png: result.png)
    )
}

private func writeError(code: String, message: String, retryable: Bool) {
    FileHandle.standardOutput.write(
        ScreenCaptureWire.failure(code: code, message: message, retryable: retryable)
    )
}

@main
private enum AgentRuntimeScreenCaptureMain {
    static func main() async {
        do {
            let request = try parseArguments(Array(CommandLine.arguments.dropFirst()))
            let result = try await ScreenCaptureService.capture(request)
            try writeSuccess(result)
            exit(0)
        } catch let error as ScreenCaptureError {
            writeError(
                code: error.code,
                message: error.message,
                retryable: error.retryable
            )
            exit(2)
        } catch let error as CLIError {
            let message: String
            switch error {
            case .invalid(let detail):
                message = detail
            }
            writeError(
                code: "INVALID_ARGUMENT",
                message: message,
                retryable: false
            )
            exit(2)
        } catch {
            writeError(
                code: "INTERNAL_ERROR",
                message: "Screen capture failed.",
                retryable: false
            )
            exit(2)
        }
    }
}
