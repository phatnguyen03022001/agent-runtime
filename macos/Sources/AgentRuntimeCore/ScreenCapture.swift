import AppKit
import CoreGraphics
import CoreImage
import CoreMedia
import CryptoKit
import Foundation
import ScreenCaptureKit

public enum ScreenCaptureError: Error, Equatable {
    case permissionRequired
    case targetNotFound
    case targetAmbiguous
    case invalidArgument(String)
    case payloadTooLarge
    case deadlineExceeded
    case protocolFailure(String)
    case captureFailed(String)

    public var code: String {
        switch self {
        case .permissionRequired: "SCREEN_CAPTURE_PERMISSION_REQUIRED"
        case .targetNotFound: "CAPTURE_TARGET_NOT_FOUND"
        case .targetAmbiguous: "CAPTURE_TARGET_AMBIGUOUS"
        case .invalidArgument: "INVALID_ARGUMENT"
        case .payloadTooLarge: "CAPTURE_PAYLOAD_TOO_LARGE"
        case .deadlineExceeded: "DEADLINE_EXCEEDED"
        case .protocolFailure: "CAPTURE_PROTOCOL_ERROR"
        case .captureFailed: "INTERNAL_ERROR"
        }
    }

    public var message: String {
        switch self {
        case .permissionRequired:
            "Screen Recording permission is required."
        case .targetNotFound:
            "The requested capture target is unavailable."
        case .targetAmbiguous:
            "The requested capture target is ambiguous."
        case .invalidArgument(let detail),
             .protocolFailure(let detail),
             .captureFailed(let detail):
            String(detail.prefix(256))
        case .payloadTooLarge:
            "The requested capture exceeds the fixed payload limit."
        case .deadlineExceeded:
            "Screen capture exceeded its fixed deadline."
        }
    }

    public var retryable: Bool { self == .deadlineExceeded }
}

public struct ScreenCaptureRect: Codable, Equatable, Sendable {
    public let x: Double
    public let y: Double
    public let width: Double
    public let height: Double

    public init(x: Double, y: Double, width: Double, height: Double) {
        self.x = x
        self.y = y
        self.width = width
        self.height = height
    }

    var cgRect: CGRect {
        CGRect(x: x, y: y, width: width, height: height)
    }

    init(_ rect: CGRect) {
        self.init(
            x: rect.origin.x,
            y: rect.origin.y,
            width: rect.size.width,
            height: rect.size.height
        )
    }
}

public struct ScreenCaptureApplicationMetadata: Codable, Equatable, Sendable {
    public let pid: Int32
    public let bundleIdentifier: String?
    public let name: String

    public init(pid: Int32, bundleIdentifier: String?, name: String) {
        self.pid = pid
        self.bundleIdentifier = bundleIdentifier
        self.name = name
    }

    init(_ application: SCRunningApplication) {
        self.init(
            pid: application.processID,
            bundleIdentifier: application.bundleIdentifier,
            name: application.applicationName
        )
    }

    init(_ application: NSRunningApplication) {
        self.init(
            pid: application.processIdentifier,
            bundleIdentifier: application.bundleIdentifier,
            name: application.localizedName ?? "Unknown"
        )
    }
}

public enum ScreenCaptureTarget: String, Codable, Sendable {
    case frontmostWindow = "frontmost_window"
    case window
    case applicationWindow = "application_window"
    case display
    case region
}

public struct ScreenCaptureRequest: Equatable, Sendable {
    public let target: ScreenCaptureTarget
    public let windowID: CGWindowID?
    public let applicationBundleID: String?
    public let displayID: CGDirectDisplayID?
    public let region: ScreenCaptureRect?

    public init(
        target: ScreenCaptureTarget,
        windowID: CGWindowID? = nil,
        applicationBundleID: String? = nil,
        displayID: CGDirectDisplayID? = nil,
        region: ScreenCaptureRect? = nil
    ) {
        self.target = target
        self.windowID = windowID
        self.applicationBundleID = applicationBundleID
        self.displayID = displayID
        self.region = region
    }
}

public struct ScreenCaptureMetadata: Codable, Equatable, Sendable {
    public let schemaVersion: Int
    public let status: String
    public let target: ScreenCaptureTarget
    public let mimeType: String
    public let rawBytes: Int
    public let sha256: String
    public let coordinateSpace: String
    public let bounds: ScreenCaptureRect
    public let pixelWidth: Int
    public let pixelHeight: Int
    public let scaleFactor: Double
    public let displayID: CGDirectDisplayID
    public let windowID: CGWindowID?
    public let activeApplication: ScreenCaptureApplicationMetadata
    public let capturedApplication: ScreenCaptureApplicationMetadata?
    public let permission: String
    public let captureAPI: String
    public let deadlineSeconds: Double

    private enum CodingKeys: String, CodingKey {
        case schemaVersion
        case status
        case target
        case mimeType
        case rawBytes
        case sha256
        case coordinateSpace
        case bounds
        case pixelWidth
        case pixelHeight
        case scaleFactor
        case displayID
        case windowID
        case activeApplication
        case capturedApplication
        case permission
        case captureAPI
        case deadlineSeconds
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(schemaVersion, forKey: .schemaVersion)
        try container.encode(status, forKey: .status)
        try container.encode(target, forKey: .target)
        try container.encode(mimeType, forKey: .mimeType)
        try container.encode(rawBytes, forKey: .rawBytes)
        try container.encode(sha256, forKey: .sha256)
        try container.encode(coordinateSpace, forKey: .coordinateSpace)
        try container.encode(bounds, forKey: .bounds)
        try container.encode(pixelWidth, forKey: .pixelWidth)
        try container.encode(pixelHeight, forKey: .pixelHeight)
        try container.encode(scaleFactor, forKey: .scaleFactor)
        try container.encode(displayID, forKey: .displayID)
        try container.encode(windowID, forKey: .windowID)
        try container.encode(activeApplication, forKey: .activeApplication)
        try container.encode(capturedApplication, forKey: .capturedApplication)
        try container.encode(permission, forKey: .permission)
        try container.encode(captureAPI, forKey: .captureAPI)
        try container.encode(deadlineSeconds, forKey: .deadlineSeconds)
    }
}

public struct ScreenCaptureResult: Sendable {
    public let metadata: ScreenCaptureMetadata
    public let png: Data
}

struct CaptureDisplayCandidate: Equatable, Sendable {
    let id: CGDirectDisplayID
    let bounds: CGRect
    let scale: Double
}

struct CaptureWindowCandidate: Equatable, Sendable {
    let id: CGWindowID
    let pid: Int32
    let layer: Int
    let onScreen: Bool
}

enum ScreenCaptureGeometry {
    static let maxPNGBytes = 16 * 1024 * 1024
    static let maxPixels = 16 * 1024 * 1024

    static func intersectionArea(_ lhs: CGRect, _ rhs: CGRect) -> Double {
        let intersection = lhs.intersection(rhs)
        guard !intersection.isNull, !intersection.isEmpty else { return 0 }
        return intersection.width * intersection.height
    }

    static func contains(_ region: CGRect, in display: CGRect) -> Bool {
        guard region.width > 0, region.height > 0 else { return false }
        return display.contains(region)
    }

    static func selectDisplay(
        for bounds: CGRect,
        candidates: [CaptureDisplayCandidate]
    ) throws -> CaptureDisplayCandidate {
        var best: CaptureDisplayCandidate?
        var bestArea = 0.0
        var tied = false
        for candidate in candidates {
            let area = intersectionArea(bounds, candidate.bounds)
            if area > bestArea {
                best = candidate
                bestArea = area
                tied = false
            } else if area > 0, abs(area - bestArea) < 0.000_001 {
                tied = true
            }
        }
        guard bestArea > 0, let best else {
            throw ScreenCaptureError.targetNotFound
        }
        if tied {
            throw ScreenCaptureError.targetAmbiguous
        }
        return best
    }

    static func pixelSize(
        for bounds: CGRect,
        scale: Double
    ) throws -> (width: Int, height: Int) {
        guard bounds.width > 0, bounds.height > 0,
              scale.isFinite, scale > 0 else {
            throw ScreenCaptureError.invalidArgument("Capture geometry must be finite and positive.")
        }
        let width = Int((bounds.width * scale).rounded())
        let height = Int((bounds.height * scale).rounded())
        guard width > 0, height > 0 else {
            throw ScreenCaptureError.invalidArgument("Capture geometry resolves to an empty image.")
        }
        guard width <= maxPixels, height <= maxPixels,
              width <= maxPixels / height else {
            throw ScreenCaptureError.payloadTooLarge
        }
        return (width, height)
    }

    static func globalPoint(
        pixelX: Double,
        pixelY: Double,
        bounds: CGRect,
        scale: Double
    ) -> CGPoint {
        CGPoint(
            x: bounds.minX + pixelX / scale,
            y: bounds.minY + pixelY / scale
        )
    }

    static func topmostWindowID(
        for pid: Int32,
        zOrdered: [CaptureWindowCandidate],
        shareableIDs: Set<CGWindowID>
    ) throws -> CGWindowID {
        guard let match = zOrdered.first(where: {
            $0.pid == pid && $0.layer == 0 && $0.onScreen && shareableIDs.contains($0.id)
        }) else {
            throw ScreenCaptureError.targetNotFound
        }
        return match.id
    }

    static func displayScale(displayID: CGDirectDisplayID, bounds: CGRect) throws -> Double {
        guard bounds.width > 0, bounds.height > 0,
              let mode = CGDisplayCopyDisplayMode(displayID) else {
            throw ScreenCaptureError.targetNotFound
        }
        let scaleX = Double(mode.pixelWidth) / bounds.width
        let scaleY = Double(mode.pixelHeight) / bounds.height
        guard scaleX.isFinite, scaleY.isFinite, scaleX > 0, scaleY > 0,
              abs(scaleX - scaleY) < 0.000_001 else {
            throw ScreenCaptureError.protocolFailure("Display scale is not uniform.")
        }
        return scaleX
    }
}

private struct ResolvedCapture {
    let filter: SCContentFilter
    let configuration: SCStreamConfiguration
    let target: ScreenCaptureTarget
    let bounds: CGRect
    let scale: Double
    let displayID: CGDirectDisplayID
    let windowID: CGWindowID?
    let activeApplication: ScreenCaptureApplicationMetadata
    let capturedApplication: ScreenCaptureApplicationMetadata?
}

private enum ScreenCaptureSystem {
    static func activeApplication() throws -> NSRunningApplication {
        guard let application = NSWorkspace.shared.frontmostApplication else {
            throw ScreenCaptureError.targetNotFound
        }
        return application
    }

    static func zOrderedWindows() -> [CaptureWindowCandidate] {
        guard let raw = CGWindowListCopyWindowInfo(
            [.optionOnScreenOnly, .excludeDesktopElements],
            kCGNullWindowID
        ) as? [[String: Any]] else {
            return []
        }
        return raw.compactMap { item in
            guard
                let id = item[kCGWindowNumber as String] as? NSNumber,
                let pid = item[kCGWindowOwnerPID as String] as? NSNumber,
                let layer = item[kCGWindowLayer as String] as? NSNumber
            else {
                return nil
            }
            return CaptureWindowCandidate(
                id: CGWindowID(id.uint32Value),
                pid: pid.int32Value,
                layer: layer.intValue,
                onScreen: true
            )
        }
    }

    static func displayCandidate(_ display: SCDisplay) throws -> CaptureDisplayCandidate {
        CaptureDisplayCandidate(
            id: display.displayID,
            bounds: display.frame,
            scale: try ScreenCaptureGeometry.displayScale(
                displayID: display.displayID,
                bounds: display.frame
            )
        )
    }
}

public enum ScreenCaptureService {
    public static let deadlineSeconds = 5.0

    public static func capture(_ request: ScreenCaptureRequest) async throws -> ScreenCaptureResult {
        guard CGPreflightScreenCaptureAccess() else {
            throw ScreenCaptureError.permissionRequired
        }
        let resolved = try await resolve(request)
        let image: CGImage
        if #available(macOS 14.0, *) {
            do {
                image = try await SCScreenshotManager.captureImage(
                    contentFilter: resolved.filter,
                    configuration: resolved.configuration
                )
            } catch {
                throw ScreenCaptureError.captureFailed("ScreenCaptureKit capture failed.")
            }
        } else {
            image = try captureLegacy(
                filter: resolved.filter,
                configuration: resolved.configuration
            )
        }
        let png = try encodePNG(image)
        guard png.count <= ScreenCaptureGeometry.maxPNGBytes else {
            throw ScreenCaptureError.payloadTooLarge
        }
        let digest = SHA256.hash(data: png).map { String(format: "%02x", $0) }.joined()
        let metadata = ScreenCaptureMetadata(
            schemaVersion: 1,
            status: "captured",
            target: resolved.target,
            mimeType: "image/png",
            rawBytes: png.count,
            sha256: digest,
            coordinateSpace: "cg_global_points",
            bounds: ScreenCaptureRect(resolved.bounds),
            pixelWidth: image.width,
            pixelHeight: image.height,
            scaleFactor: resolved.scale,
            displayID: resolved.displayID,
            windowID: resolved.windowID,
            activeApplication: resolved.activeApplication,
            capturedApplication: resolved.capturedApplication,
            permission: "granted",
            captureAPI: "ScreenCaptureKit",
            deadlineSeconds: deadlineSeconds
        )
        let expected = try ScreenCaptureGeometry.pixelSize(
            for: resolved.bounds,
            scale: resolved.scale
        )
        guard image.width == expected.width, image.height == expected.height else {
            throw ScreenCaptureError.protocolFailure("Captured pixel dimensions are inconsistent.")
        }
        return ScreenCaptureResult(metadata: metadata, png: png)
    }

    private static func resolve(_ request: ScreenCaptureRequest) async throws -> ResolvedCapture {
        let content: SCShareableContent
        do {
            content = try await SCShareableContent.excludingDesktopWindows(
                true,
                onScreenWindowsOnly: true
            )
        } catch {
            throw ScreenCaptureError.captureFailed("Shareable screen content is unavailable.")
        }
        let active = try ScreenCaptureSystem.activeApplication()
        let activeMetadata = ScreenCaptureApplicationMetadata(active)

        switch request.target {
        case .frontmostWindow:
            guard request.windowID == nil, request.applicationBundleID == nil,
                  request.displayID == nil, request.region == nil else {
                throw ScreenCaptureError.invalidArgument("frontmost_window accepts no selector.")
            }
            return try resolveWindow(
                pid: active.processIdentifier,
                explicitWindowID: nil,
                target: request.target,
                content: content,
                active: activeMetadata
            )

        case .window:
            guard let windowID = request.windowID,
                  request.applicationBundleID == nil,
                  request.displayID == nil,
                  request.region == nil else {
                throw ScreenCaptureError.invalidArgument("window requires exactly window_id.")
            }
            return try resolveWindow(
                pid: nil,
                explicitWindowID: windowID,
                target: request.target,
                content: content,
                active: activeMetadata
            )

        case .applicationWindow:
            guard let bundleID = request.applicationBundleID,
                  request.windowID == nil, request.displayID == nil,
                  request.region == nil else {
                throw ScreenCaptureError.invalidArgument(
                    "application_window requires exactly application_bundle_id."
                )
            }
            let applications = NSRunningApplication
                .runningApplications(withBundleIdentifier: bundleID)
                .filter { !$0.isTerminated }
            guard applications.count == 1, let application = applications.first else {
                throw applications.isEmpty
                    ? ScreenCaptureError.targetNotFound
                    : ScreenCaptureError.targetAmbiguous
            }
            return try resolveWindow(
                pid: application.processIdentifier,
                explicitWindowID: nil,
                target: request.target,
                content: content,
                active: activeMetadata
            )

        case .display:
            guard let displayID = request.displayID,
                  request.windowID == nil, request.applicationBundleID == nil,
                  request.region == nil else {
                throw ScreenCaptureError.invalidArgument("display requires exactly display_id.")
            }
            guard let display = content.displays.first(where: { $0.displayID == displayID }) else {
                throw ScreenCaptureError.targetNotFound
            }
            return try resolveDisplay(
                display: display,
                target: request.target,
                bounds: display.frame,
                active: activeMetadata
            )

        case .region:
            guard let displayID = request.displayID, let region = request.region,
                  request.windowID == nil, request.applicationBundleID == nil else {
                throw ScreenCaptureError.invalidArgument(
                    "region requires display_id and finite x, y, width, height."
                )
            }
            guard let display = content.displays.first(where: { $0.displayID == displayID }) else {
                throw ScreenCaptureError.targetNotFound
            }
            let rect = region.cgRect
            guard rect.origin.x.isFinite, rect.origin.y.isFinite,
                  rect.width.isFinite, rect.height.isFinite,
                  ScreenCaptureGeometry.contains(rect, in: display.frame) else {
                throw ScreenCaptureError.invalidArgument(
                    "region must be positive and fully contained in the selected display."
                )
            }
            return try resolveDisplay(
                display: display,
                target: request.target,
                bounds: rect,
                active: activeMetadata
            )
        }
    }

    private static func resolveWindow(
        pid: Int32?,
        explicitWindowID: CGWindowID?,
        target: ScreenCaptureTarget,
        content: SCShareableContent,
        active: ScreenCaptureApplicationMetadata
    ) throws -> ResolvedCapture {
        let shareableIDs = Set(content.windows.map(\.windowID))
        let selectedID: CGWindowID
        if let explicitWindowID {
            selectedID = explicitWindowID
        } else if let pid {
            selectedID = try ScreenCaptureGeometry.topmostWindowID(
                for: pid,
                zOrdered: ScreenCaptureSystem.zOrderedWindows(),
                shareableIDs: shareableIDs
            )
        } else {
            throw ScreenCaptureError.invalidArgument("A window selector is required.")
        }
        let matches = content.windows.filter { $0.windowID == selectedID }
        guard matches.count == 1, let window = matches.first else {
            throw matches.isEmpty
                ? ScreenCaptureError.targetNotFound
                : ScreenCaptureError.targetAmbiguous
        }
        guard window.windowLayer == 0, window.isOnScreen else {
            throw ScreenCaptureError.targetNotFound
        }
        let candidates = try content.displays.map(ScreenCaptureSystem.displayCandidate)
        let display = try ScreenCaptureGeometry.selectDisplay(
            for: window.frame,
            candidates: candidates
        )
        guard let screenDisplay = content.displays.first(where: { $0.displayID == display.id }) else {
            throw ScreenCaptureError.targetNotFound
        }
        let size = try ScreenCaptureGeometry.pixelSize(for: window.frame, scale: display.scale)
        _ = screenDisplay
        let configuration = SCStreamConfiguration()
        configuration.width = size.width
        configuration.height = size.height
        configuration.showsCursor = false
        configuration.capturesAudio = false
        configuration.queueDepth = 1
        if #available(macOS 14.0, *) {
            configuration.ignoreShadowsSingleWindow = true
        }
        let captured = window.owningApplication.map(ScreenCaptureApplicationMetadata.init)
        return ResolvedCapture(
            filter: SCContentFilter(desktopIndependentWindow: window),
            configuration: configuration,
            target: target,
            bounds: window.frame,
            scale: display.scale,
            displayID: display.id,
            windowID: window.windowID,
            activeApplication: active,
            capturedApplication: captured
        )
    }

    private static func resolveDisplay(
        display: SCDisplay,
        target: ScreenCaptureTarget,
        bounds: CGRect,
        active: ScreenCaptureApplicationMetadata
    ) throws -> ResolvedCapture {
        let candidate = try ScreenCaptureSystem.displayCandidate(display)
        let size = try ScreenCaptureGeometry.pixelSize(for: bounds, scale: candidate.scale)
        let configuration = SCStreamConfiguration()
        configuration.width = size.width
        configuration.height = size.height
        configuration.showsCursor = false
        configuration.capturesAudio = false
        configuration.queueDepth = 1
        if #available(macOS 14.0, *) {
            configuration.ignoreShadowsDisplay = true
        }
        configuration.sourceRect = CGRect(
            x: bounds.minX - display.frame.minX,
            y: bounds.minY - display.frame.minY,
            width: bounds.width,
            height: bounds.height
        )
        return ResolvedCapture(
            filter: SCContentFilter(display: display, excludingWindows: []),
            configuration: configuration,
            target: target,
            bounds: bounds,
            scale: candidate.scale,
            displayID: display.displayID,
            windowID: nil,
            activeApplication: active,
            capturedApplication: nil
        )
    }

    private static func encodePNG(_ image: CGImage) throws -> Data {
        let representation = NSBitmapImageRep(cgImage: image)
        guard let data = representation.representation(using: .png, properties: [:]) else {
            throw ScreenCaptureError.protocolFailure("Native PNG encoding failed.")
        }
        return data
    }

    private static func captureLegacy(
        filter: SCContentFilter,
        configuration: SCStreamConfiguration
    ) throws -> CGImage {
        let receiver = LegacyFrameReceiver()
        let stream = SCStream(filter: filter, configuration: configuration, delegate: receiver)
        do {
            try stream.addStreamOutput(
                receiver,
                type: .screen,
                sampleHandlerQueue: receiver.queue
            )
        } catch {
            throw ScreenCaptureError.captureFailed("ScreenCaptureKit output setup failed.")
        }
        stream.startCapture { error in
            receiver.recordStart(error)
        }
        return try receiver.waitForFrame(stream: stream)
    }
}

private final class LegacyFrameReceiver: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    let queue = DispatchQueue(label: "com.picmao.agent-runtime.screen-capture-frame")
    private let startSignal = DispatchSemaphore(value: 0)
    private let frameSignal = DispatchSemaphore(value: 0)
    private let lock = NSLock()
    private let context = CIContext()
    private var startError: Error?
    private var frame: CGImage?
    private var frameError: Error?
    private var frameRecorded = false

    func recordStart(_ error: Error?) {
        lock.lock()
        startError = error
        lock.unlock()
        startSignal.signal()
    }

    func stream(
        _ stream: SCStream,
        didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
        of outputType: SCStreamOutputType
    ) {
        guard outputType == .screen else { return }
        lock.lock()
        let shouldRecord = !frameRecorded
        if shouldRecord { frameRecorded = true }
        lock.unlock()
        guard shouldRecord else { return }

        guard sampleBuffer.isValid,
              let buffer = CMSampleBufferGetImageBuffer(sampleBuffer) else {
            recordFrame(error: ScreenCaptureError.protocolFailure("Invalid screen frame."))
            return
        }
        let source = CIImage(cvPixelBuffer: buffer)
        guard let image = context.createCGImage(source, from: source.extent) else {
            recordFrame(error: ScreenCaptureError.protocolFailure("Could not materialize screen frame."))
            return
        }
        recordFrame(image: image)
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        recordFrame(error: error)
    }

    private func recordFrame(image: CGImage? = nil, error: Error? = nil) {
        lock.lock()
        if frame == nil && frameError == nil {
            frame = image
            frameError = error
        }
        lock.unlock()
        frameSignal.signal()
    }

    func waitForFrame(stream: SCStream) throws -> CGImage {
        guard startSignal.wait(timeout: .now() + 1.5) == .success else {
            stop(stream)
            throw ScreenCaptureError.deadlineExceeded
        }
        lock.lock()
        let startupError = startError
        lock.unlock()
        if startupError != nil {
            stop(stream)
            throw ScreenCaptureError.captureFailed("ScreenCaptureKit stream could not start.")
        }

        guard frameSignal.wait(timeout: .now() + 2.5) == .success else {
            stop(stream)
            throw ScreenCaptureError.deadlineExceeded
        }
        lock.lock()
        let image = frame
        let error = frameError
        lock.unlock()
        stop(stream)
        if error != nil {
            throw ScreenCaptureError.captureFailed("ScreenCaptureKit stream stopped before a frame.")
        }
        guard let image else {
            throw ScreenCaptureError.protocolFailure("ScreenCaptureKit produced no image.")
        }
        return image
    }

    private func stop(_ stream: SCStream) {
        let stopped = DispatchSemaphore(value: 0)
        stream.stopCapture { _ in stopped.signal() }
        _ = stopped.wait(timeout: .now() + 0.25)
    }
}

private struct ScreenCaptureErrorHeader: Encodable {
    let status = "error"
    let errorCode: String
    let message: String
    let retryable: Bool
}

public enum ScreenCaptureWire {
    public static let maxHeaderBytes = 16 * 1024

    private static func encoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        return encoder
    }

    public static func success(
        metadata: ScreenCaptureMetadata,
        png: Data
    ) throws -> Data {
        let header = try encoder().encode(metadata)
        guard header.count <= maxHeaderBytes else {
            throw ScreenCaptureError.protocolFailure(
                "Screen capture metadata exceeded its fixed bound."
            )
        }
        var output = Data(capacity: header.count + 1 + png.count)
        output.append(header)
        output.append(0x0A)
        output.append(png)
        return output
    }

    public static func failure(
        code: String,
        message: String,
        retryable: Bool
    ) -> Data {
        let header = ScreenCaptureErrorHeader(
            errorCode: code,
            message: String(message.prefix(256)),
            retryable: retryable
        )
        guard let encoded = try? encoder().encode(header),
              encoded.count <= maxHeaderBytes else {
            return Data()
        }
        var output = Data(capacity: encoded.count + 1)
        output.append(encoded)
        output.append(0x0A)
        return output
    }
}
