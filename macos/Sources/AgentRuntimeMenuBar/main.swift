import AppKit
import Darwin

if let status = ServiceManagementCommand.run(arguments: CommandLine.arguments) {
    exit(status)
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.run()
