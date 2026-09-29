# Installation

This document separates the supported prebuilt-consumer path from the build-from-source maintainer path. Both paths converge on the same sealed candidate format, canonical Runtime configuration, and transactional cutover semantics.

## Prebuilt release installation

A qualified extracted release bundle has this minimal shape:

```text
Agent Runtime.app/
Agent Runtime.candidate.json
payloads/<initial-closure>/
```

The checkout-independent installer is sealed inside the app:

```text
Agent Runtime.app/Contents/Resources/runtime/macos/install_release.sh
```

The [v0.5.1 zero-cost release](https://github.com/phatnguyen03022001/agent-runtime/releases/tag/v0.5.1) provides this complete bundle for Apple silicon Macs. Download the archive and `SHA256SUMS.txt` together; from their directory run `shasum -a 256 -c SHA256SUMS.txt` before extracting the archive. The accepted archive SHA-256 is `9518e1cf4f28033cbec97b8906e55e1a4da6ad3a9067485fb6ef7c4fcfac58e8`; the tag points to packaged source revision `674095e739533e100c2cf75f434dd0bc825a3de3`. Keep the extracted app, handoff and payload together. This ad-hoc build does not claim an Apple-authenticated publisher or notarization.

### Target prerequisites

The target operator needs only:

- macOS on Apple silicon;
- the official OpenAI `tunnel-client`;
- ordinary macOS lifecycle tools used by the existing Runtime lifecycle;
- an explicit existing absolute workspace root;
- provisioned `CONTROL_PLANE_API_KEY` and `CONTROL_PLANE_TUNNEL_ID`;
- an explicit Runtime Git name/email pair.

The first ad-hoc app launch may require macOS Open/Open Anyway for that exact app. The Runtime supervisor uses the traditional per-user LaunchAgent and does not require current ServiceManagement Background Activity registration. Separately, installation establishes `SMAppService.mainApp` for menu-bar login startup; a macOS `requires-approval` state is surfaced as explicit human action required rather than accepted as healthy.

The target does **not** need an agent-runtime Git clone, Git repository identity for an agent-runtime checkout, CPython 3.13, Xcode/Swift, or a local code-signing identity. The release installer uses the Python interpreter and lifecycle helpers packaged inside `Agent Runtime.app`.

OpenAI Secure MCP Tunnel guidance and `tunnel-client` acquisition are:
https://developers.openai.com/api/docs/guides/secure-mcp-tunnels

### First-run native setup

Keep `Agent Runtime.app`, `Agent Runtime.candidate.json`, and the `payloads` directory together and open the app. A fresh prebuilt launch with no usable canonical configuration presents setup automatically.

1. Enter the provisioned `CONTROL_PLANE_API_KEY` in the secure API-key field.
2. Enter the provisioned `CONTROL_PLANE_TUNNEL_ID` in the secure tunnel field.
3. Provide the Runtime Git name/email pair when setup requires it.
4. Choose an existing workspace with the macOS directory picker. The normal path does not accept a typed workspace path.
5. Continue setup. If macOS blocks the initial ad-hoc app launch, use its Open/Open Anyway control for this exact app, then reopen the complete bundle.

Secrets cross from the native UI to the packaged configuration helper only through bounded stdin. They are not placed in argv, shell exports, UserDefaults, logs, diagnostics, or repository evidence. The canonical destination remains:

```text
~/Library/Application Support/Agent Runtime/runtime.env
```

The existing `runtime_config.py` authority validates all values before publication and creates the canonical file privately and atomically at mode `0600`. Invalid input creates no partial canonical file and starts no cutover. An existing canonical file is inspected and preserved; setup never silently repairs or overwrites an invalid/unsafe existing file.

The native flow then delegates to the packaged `install_release.sh`, provenance, and `candidate_cutover.py` implementation. The external candidate handoff and matching initial content-addressed payload are mandatory and must remain beside the app. Missing, unsafe, or mismatched state fails closed with guidance to reopen the complete release bundle.

Before commit, setup requires:

- Runtime readiness `ready`;
- zero doctor failures; and
- exactly one doctor warning: `cutover_identity/CUTOVER_TRANSACTION_PRESENT` for the recognized `PENDING` / `APP_SWAPPED` transaction.

The package-owned doctor therefore remains `degraded` at that boundary solely because the transaction still exists. Any additional warning or failure blocks commit. Only then may setup invoke the existing cutover commit. Terminal success additionally requires the transaction to be absent, readiness to remain `ready`, and package-owned doctor overall status to be `healthy`.

The app and initial payload are validated before cutover. Setup does not bypass Gatekeeper or install or update the external `tunnel-client`. Native/substrate cutover requires coherent lifecycle ownership: `SMAppService.mainApp` for menu-bar login startup, the canonical per-user LaunchAgent for the Runtime supervisor, and no current Runtime `SMAppService.agent` registration. Ordinary validated pure-Python payload activation changes the external release pointer only after the previous Runtime generation is fully stopped and reaped; it leaves the approved app, LaunchAgent plist, runtime.env, and tunnel-client unchanged.

An already-valid installed configuration with no pending cutover bypasses onboarding and opens the current control panel directly.

### Packaged lifecycle CLI — maintainers and recovery

The sealed installer remains available as the low-level package/recovery interface; it is **not** a required fresh-user step:

```bash
RELEASE_INSTALLER="./Agent Runtime.app/Contents/Resources/runtime/macos/install_release.sh"
"$RELEASE_INSTALLER" --resume-cutover
"$RELEASE_INSTALLER" --commit-cutover
"$RELEASE_INSTALLER" --rollback-cutover
"$RELEASE_INSTALLER" --recover-partial-cutover
```

These commands preserve the same transaction authority used by native setup. Do not delete or synthesize cutover metadata to force progress.

## Build from source

The existing source-build path remains the maintainer path and is not made dependent on Apple notarization.

### Maintainer prerequisites

Source packaging requires Git, `launchctl`, `lsof`, `curl`, `xcrun`, Swift, the canonical CPython 3.13 arm64 packaging interpreter, and the official OpenAI `tunnel-client`. It uses ad-hoc signing with deterministic responsible-code identifiers.

Apple's Command Line Tools guidance is:
https://developer.apple.com/documentation/xcode/installing-the-command-line-tools

The preflight never installs/selects developer tools or enumerates Keychain identities. No signing identity or notary profile is required.

Create checkout configuration from `.env.example`, mode `0600`, and provide the required transport/Runtime values. The source installer derives the canonical workspace from the checkout parent and may use repository-local Git identity only on this maintainer path.

Run the read-only source preflight:

```bash
./install.sh --check
./install.sh --check --json
```

When it is `READY`:

```bash
./install.sh
```

This source lane verifies source, builds/signs a local candidate, initializes canonical configuration, validates tunnel configuration, and enters the same transactional cutover. It also remains pending until explicit commit or rollback:

```bash
./install.sh --commit-cutover
# or
./install.sh --rollback-cutover
```

## Distribution packaging authority

The supported packaging command is explicit:

```bash
./macos/package_app.sh --zero-cost
```

It stages one exact clean Git source candidate, builds the immutable app substrate, signs native code ad-hoc with deterministic identifiers, publishes a first-party pure-Python release under its content closure, then seals and validates the external candidate handoff. The complete distribution consists of the app, handoff, and initial `payloads/<closure>` release. Ad-hoc signing proves integrity only; it supplies no TeamIdentifier or publisher authentication. A native/substrate change requires a separate app release and may require Gatekeeper approval again. No Developer ID, notarization, stapling, sudo, quarantine deletion, or global Gatekeeper disable is part of this lane.

## Installed-authority operation

The installed Runtime remains package-owned under `~/Applications/Agent Runtime.app`. Canonical `runtime.env` is retained by default during uninstall. Credentials and Git identity values must never be printed or logged.

The official `tunnel-client` continues to own the outbound HTTPS connection to OpenAI and the protected loopback listener at `127.0.0.1:8080`. The release installer does not register arbitrary tunnels or broaden that identity boundary.

For recovery semantics after any pending or partial cutover, follow [RECOVERY.md](RECOVERY.md).
