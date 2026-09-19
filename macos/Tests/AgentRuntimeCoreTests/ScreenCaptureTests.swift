import CoreGraphics
import Foundation
import XCTest
@testable import AgentRuntimeCore

final class ScreenCaptureTests: XCTestCase {
    func testTopmostNormalWindowSelectionUsesFrontToBackOrder() throws {
        let windows = [
            CaptureWindowCandidate(id: 40, pid: 7, layer: 1, onScreen: true),
            CaptureWindowCandidate(id: 41, pid: 8, layer: 0, onScreen: true),
            CaptureWindowCandidate(id: 42, pid: 7, layer: 0, onScreen: true),
            CaptureWindowCandidate(id: 43, pid: 7, layer: 0, onScreen: true),
        ]
        XCTAssertEqual(
            try ScreenCaptureGeometry.topmostWindowID(
                for: 7,
                zOrdered: windows,
                shareableIDs: [42, 43]
            ),
            42
        )
    }

    func testRegionContainmentAcceptsNegativeGlobalCoordinates() {
        let display = CGRect(x: -1920, y: -100, width: 1920, height: 1080)
        let inside = CGRect(x: -1800, y: 0, width: 400, height: 300)
        let outside = CGRect(x: -2000, y: 0, width: 400, height: 300)
        XCTAssertTrue(ScreenCaptureGeometry.contains(inside, in: display))
        XCTAssertFalse(ScreenCaptureGeometry.contains(outside, in: display))
    }

    func testWindowDisplaySelectionUsesGreatestIntersectionAndRejectsTie() throws {
        let left = CaptureDisplayCandidate(
            id: 1,
            bounds: CGRect(x: -100, y: 0, width: 100, height: 100),
            scale: 2
        )
        let right = CaptureDisplayCandidate(
            id: 2,
            bounds: CGRect(x: 0, y: 0, width: 100, height: 100),
            scale: 2
        )
        XCTAssertEqual(
            try ScreenCaptureGeometry.selectDisplay(
                for: CGRect(x: -25, y: 0, width: 100, height: 100),
                candidates: [left, right]
            ).id,
            2
        )
        XCTAssertThrowsError(
            try ScreenCaptureGeometry.selectDisplay(
                for: CGRect(x: -50, y: 0, width: 100, height: 100),
                candidates: [left, right]
            )
        ) { error in
            XCTAssertEqual(error as? ScreenCaptureError, .targetAmbiguous)
        }
    }

    func testPixelCoordinateMappingIsExplicitGlobalPoints() throws {
        let bounds = CGRect(x: -120, y: 35, width: 20, height: 10)
        let size = try ScreenCaptureGeometry.pixelSize(for: bounds, scale: 2)
        XCTAssertEqual(size.width, 40)
        XCTAssertEqual(size.height, 20)
        XCTAssertEqual(
            ScreenCaptureGeometry.globalPoint(
                pixelX: 10,
                pixelY: 6,
                bounds: bounds,
                scale: 2
            ),
            CGPoint(x: -115, y: 38)
        )
    }

    func testPixelLimitFailsWithoutDownscaling() {
        XCTAssertThrowsError(
            try ScreenCaptureGeometry.pixelSize(
                for: CGRect(x: 0, y: 0, width: 5000, height: 5000),
                scale: 1
            )
        ) { error in
            XCTAssertEqual(error as? ScreenCaptureError, .payloadTooLarge)
        }
    }

    func testPermissionFailureMappingIsStableAndNotRetryable() {
        let error = ScreenCaptureError.permissionRequired
        XCTAssertEqual(error.code, "SCREEN_CAPTURE_PERMISSION_REQUIRED")
        XCTAssertEqual(error.message, "Screen Recording permission is required.")
        XCTAssertFalse(error.retryable)
    }

    func testWireFramingIsOneJSONLineThenExactPNG() throws {
        let png = Data([0x89, 0x50, 0x4E, 0x47, 0x01, 0x02])
        let app = ScreenCaptureApplicationMetadata(
            pid: 7,
            bundleIdentifier: "com.example.app",
            name: "Example"
        )
        let metadata = ScreenCaptureMetadata(
            schemaVersion: 1,
            status: "captured",
            target: .frontmostWindow,
            mimeType: "image/png",
            rawBytes: png.count,
            sha256: String(repeating: "a", count: 64),
            coordinateSpace: "cg_global_points",
            bounds: ScreenCaptureRect(x: -10, y: 20, width: 1, height: 1),
            pixelWidth: 2,
            pixelHeight: 2,
            scaleFactor: 2,
            displayID: 1,
            windowID: 9,
            activeApplication: app,
            capturedApplication: app,
            permission: "granted",
            captureAPI: "ScreenCaptureKit",
            deadlineSeconds: 5
        )
        let frame = try ScreenCaptureWire.success(metadata: metadata, png: png)
        let newline = try XCTUnwrap(frame.firstIndex(of: 0x0A))
        let header = frame[..<newline]
        let payload = frame[frame.index(after: newline)...]
        let object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(header)) as? [String: Any]
        )
        XCTAssertEqual(object["coordinate_space"] as? String, "cg_global_points")
        XCTAssertEqual(object["capture_api"] as? String, "ScreenCaptureKit")
        XCTAssertEqual(Data(payload), png)
    }

    func testErrorFramingHasNoBinaryPayloadAndBoundedMessage() throws {
        let frame = ScreenCaptureWire.failure(
            code: "SCREEN_CAPTURE_PERMISSION_REQUIRED",
            message: String(repeating: "x", count: 500),
            retryable: false
        )
        XCTAssertEqual(frame.last, 0x0A)
        let header = frame.dropLast()
        let object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(header)) as? [String: Any]
        )
        XCTAssertEqual(
            Set(object.keys),
            Set(["status", "error_code", "message", "retryable"])
        )
        XCTAssertEqual(object["status"] as? String, "error")
        XCTAssertEqual((object["message"] as? String)?.count, 256)
    }
}
