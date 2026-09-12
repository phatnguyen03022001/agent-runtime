import Darwin
import Foundation

final class MenuBarInstanceLock {
    private let descriptor: Int32

    init?() {
        let directory = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Agent Runtime", isDirectory: true)
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        } catch {
            return nil
        }
        let path = directory.appendingPathComponent("menu-bar.lock", isDirectory: false).path
        let opened = path.withCString { open($0, O_CREAT | O_RDWR, mode_t(0o600)) }
        guard opened >= 0 else { return nil }
        guard flock(opened, LOCK_EX | LOCK_NB) == 0 else {
            close(opened)
            return nil
        }
        descriptor = opened
    }

    deinit {
        _ = flock(descriptor, LOCK_UN)
        close(descriptor)
    }
}
