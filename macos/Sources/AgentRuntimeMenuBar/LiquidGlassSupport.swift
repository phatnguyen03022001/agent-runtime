import AppKit

/// Encapsulates native Apple Liquid Glass presentation and macOS 13 deployment floor fallback.
@MainActor
enum LiquidGlassSupport {
    /// Indicates whether `NSGlassEffectView` is supported by the runtime macOS environment.
    static var isLiquidGlassSupported: Bool {
        if #available(macOS 26.0, *) {
            return true
        } else {
            return false
        }
    }

    /// Wraps the content container in native Apple `NSGlassEffectView` on macOS 26+,
    /// or a standard `NSView` fallback on macOS 13–15.
    ///
    /// Native knob decisions:
    /// - `contentView`: Hosts the provided content view as the supported boundary without arbitrary subview z-order hacks.
    /// - `style`: Uses `.regular` to guarantee legibility and contrast for dense Runtime facts and controls.
    /// - `tintColor`: Leaves tintColor as `nil` (system default) to adapt smoothly to Light/Dark appearances and accessibility settings.
    /// - `cornerRadius`: Uses native popover-conforming geometry (10.0 pt).
    /// - `effectIsInteractive`: Enabled on macOS 27+ because the popover contains interactive lifecycle and quit controls.
    /// - `NSGlassEffectContainerView`: Deliberately omitted; unnecessary for a single coherent popover surface.
    static func makeBackgroundView(embedding contentView: NSView) -> NSView {
        if #available(macOS 26.0, *) {
            let glassView = NSGlassEffectView(frame: NSRect(x: 0, y: 0, width: 304, height: 1))
            glassView.style = .regular
            glassView.tintColor = nil
            glassView.cornerRadius = 10.0
            if #available(macOS 27.0, *) {
                glassView.effectIsInteractive = true
            }
            contentView.translatesAutoresizingMaskIntoConstraints = false
            glassView.contentView = contentView
            return glassView
        } else {
            contentView.translatesAutoresizingMaskIntoConstraints = false
            let fallbackView = NSView(frame: NSRect(x: 0, y: 0, width: 304, height: 1))
            fallbackView.addSubview(contentView)
            NSLayoutConstraint.activate([
                contentView.leadingAnchor.constraint(equalTo: fallbackView.leadingAnchor),
                contentView.trailingAnchor.constraint(equalTo: fallbackView.trailingAnchor),
                contentView.topAnchor.constraint(equalTo: fallbackView.topAnchor),
                contentView.bottomAnchor.constraint(equalTo: fallbackView.bottomAnchor),
            ])
            return fallbackView
        }
    }
}
