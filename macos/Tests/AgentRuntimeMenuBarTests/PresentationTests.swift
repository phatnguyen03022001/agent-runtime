import AgentRuntimeCore
@testable import AgentRuntimeMenuBar
import XCTest

final class PresentationTests: XCTestCase {
    private let servingIdentity = ProcessIdentity(
        pid: 42,
        processGroupID: 42,
        startSeconds: 1,
        startMicroseconds: 2,
        executablePath: "/usr/bin/tunnel-client"
    )

    func testOfflineAudioPlaysOncePerConnectedToOfflineEdge() {
        var policy = OfflineAudioPolicy()

        XCTAssertFalse(policy.shouldPlay(for: .stopped))
        XCTAssertFalse(policy.shouldPlay(for: .owned(servingIdentity)))
        XCTAssertFalse(policy.shouldPlay(for: .owned(servingIdentity)))
        XCTAssertTrue(policy.shouldPlay(for: .stopped))
        XCTAssertFalse(policy.shouldPlay(for: .stopped))
        XCTAssertFalse(policy.shouldPlay(for: .owned(servingIdentity)))
        XCTAssertTrue(policy.shouldPlay(for: .ambiguous("health unavailable")))
        XCTAssertFalse(policy.shouldPlay(for: .ambiguous("still unavailable")))
    }

    func testOfflineAudioIgnoresExternalAndFirstAttentionObservations() {
        var policy = OfflineAudioPolicy()

        XCTAssertFalse(policy.shouldPlay(for: .ambiguous("first observation")))
        XCTAssertFalse(policy.shouldPlay(for: .external([99])))
        XCTAssertFalse(policy.shouldPlay(for: .stopped))
        XCTAssertFalse(policy.shouldPlay(for: .external([99])))
        XCTAssertFalse(policy.shouldPlay(for: .owned(servingIdentity)))
        XCTAssertFalse(policy.shouldPlay(for: .external([99])))
        XCTAssertFalse(policy.shouldPlay(for: .external([99])))
        XCTAssertTrue(policy.shouldPlay(for: .stopped))
    }
}
