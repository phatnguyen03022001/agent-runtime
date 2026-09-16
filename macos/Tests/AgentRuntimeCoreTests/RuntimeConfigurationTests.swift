import Foundation
import XCTest
@testable import AgentRuntimeCore

final class RuntimeConfigurationTests: XCTestCase {
    func testConfiguredParallelismReadsValidOperatorMaximum() throws {
        let env = try temporaryEnv("AGENT_RUNTIME_MAX_PARALLELISM=4\n")
        XCTAssertEqual(RuntimeConfiguredParallelism.effective(from: env), 4)
    }

    func testConfiguredParallelismFallsBackOutsideOneThroughTen() throws {
        XCTAssertEqual(RuntimeConfiguredParallelism.fallback, 2)
        XCTAssertEqual(RuntimeConfiguredParallelism.effective(from: try temporaryEnv("AGENT_RUNTIME_MAX_PARALLELISM=0\n")), 2)
        XCTAssertEqual(RuntimeConfiguredParallelism.effective(from: try temporaryEnv("AGENT_RUNTIME_MAX_PARALLELISM=11\n")), 2)
        XCTAssertEqual(RuntimeConfiguredParallelism.effective(from: try temporaryEnv("AGENT_RUNTIME_MAX_PARALLELISM=oops\n")), 2)
    }

    func testRuntimeConfigurationExposesConfiguredParallelism() throws {
        let env = try temporaryEnv("AGENT_RUNTIME_MAX_PARALLELISM=4\n")
        let configuration = RuntimeConfiguration(checkoutRoot: "/tmp/runtime", envFileURL: env)
        XCTAssertEqual(configuration.parallelLimit, 4)
    }

    func testSessionCapacityIsBoundedToOneThroughSix() throws {
        XCTAssertEqual(RuntimeSessionCapacity.fallback, 6)
        XCTAssertEqual(RuntimeSessionCapacity.effective(from: try temporaryEnv("AGENT_RUNTIME_MAX_ACTIVE_SESSIONS=1\n")), 1)
        XCTAssertEqual(RuntimeSessionCapacity.effective(from: try temporaryEnv("AGENT_RUNTIME_MAX_ACTIVE_SESSIONS=6\n")), 6)
        XCTAssertEqual(RuntimeSessionCapacity.effective(from: try temporaryEnv("AGENT_RUNTIME_MAX_ACTIVE_SESSIONS=7\n")), 6)
        XCTAssertEqual(RuntimeSessionCapacity.effective(from: try temporaryEnv("AGENT_RUNTIME_MAX_ACTIVE_SESSIONS=invalid\n")), 6)
    }

    private func temporaryEnv(_ contents: String) throws -> URL {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let env = directory.appendingPathComponent("runtime.env", isDirectory: false)
        try contents.write(to: env, atomically: true, encoding: .utf8)
        addTeardownBlock { try? FileManager.default.removeItem(at: directory) }
        return env
    }
}
