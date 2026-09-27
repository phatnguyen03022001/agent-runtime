# Installation

This document separates the supported prebuilt-consumer path from the build-from-source maintainer path. Both paths converge on the same sealed candidate format, canonical Runtime configuration, and transactional cutover semantics.

## Prebuilt release installation

A qualified extracted release bundle has this minimal shape:

```text
Agent Runtime.app/
Agent Runtime.candidate.json
```

The checkout-independent installer is sealed inside the app:

```text
Agent Runtime.app/Contents/Resources/runtime/macos/install_release.sh
```

The repository implements the source architecture for this lane. It does not claim that a public notarized release has already been qualified or published; release qualification and publication require separate authority.

### Target prerequisites

The target operator needs only:

- macOS on Apple silicon;
- the official OpenAI `tunnel-client`;
- ordinary macOS lifecycle tools used by the existing Runtime lifecycle;
- an explicit existing absolute workspace root;
- provisioned `CONTROL_PLANE_API_KEY` and `CONTROL_PLANE_TUNNEL_ID`;
- an explicit Runtime Git name/email pair;
- human Background Activity approval if macOS requires it.

The target does **not** need an agent-runtime Git clone, Git repository identity for an agent-runtime checkout, CPython 3.13, Xcode/Swift, or a local code-signing identity. The release installer uses the Python interpreter and lifecycle helpers packaged inside `Agent Runtime.app`.

OpenAI Secure MCP Tunnel guidance and `tunnel-client` acquisition are:
https://developers.openai.com/api/docs/guides/secure-mcp-tunnels

### First-run native setup

Keep `Agent Runtime.app` beside `Agent Runtime.candidate.json` and open the app. A fresh prebuilt launch with no usable canonical configuration presents setup automatically.

1. Enter the provisioned `CONTROL_PLANE_API_KEY` in the secure API-key field.
2. Enter the provisioned `CONTROL_PLANE_TUNNEL_ID` in the secure tunnel field.
3. Provide the Runtime Git name/email pair when setup requires it.
4. Choose an existing workspace with the macOS directory picker. The normal path does not accept a typed workspace path.
5. Continue setup. If macOS requests Background Activity approval, complete that platform action and use the setup continuation action.

Secrets cross from the native UI to the packaged configuration helper only through bounded stdin. They are not placed in argv, shell exports, UserDefaults, logs, diagnostics, or repository evidence. The canonical destination remains:

```text
~/Library/Application Support/Agent Runtime/runtime.env
```

The existing `runtime_config.py` authority validates all values before publication and creates the canonical file privately and atomically at mode `0600`. Invalid input creates no partial canonical file and starts no cutover. An existing canonical file is inspected and preserved; setup never silently repairs or overwrites an invalid/unsafe existing file.

The native flow then delegates to the packaged `install_release.sh`, provenance, and `candidate_cutover.py` implementation. The external candidate handoff is mandatory and must remain beside the app. Missing or unsafe handoff state fails closed with guidance to reopen the complete release bundle.

Before commit, setup requires:

- Runtime readiness `ready`;
- zero doctor failures; and
- exactly one doctor warning: `cutover_identity/CUTOVER_TRANSACTION_PRESENT` for the recognized `PENDING` / `APP_SWAPPED` transaction.

The package-owned doctor therefore remains `degraded` at that boundary solely because the transaction still exists. Any additional warning or failure blocks commit. Only then may setup invoke the existing cutover commit. Terminal success additionally requires the transaction to be absent, readiness to remain `ready`, and package-owned doctor overall status to be `healthy`.

A recognized approval, partial, or otherwise non-success state stays actionable through the existing resume/rollback/recovery paths. Setup does not bypass macOS approval and does not install or update the external `tunnel-client`.

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

Source packaging requires Git, `launchctl`, `lsof`, `curl`, `xcrun`, Swift, the canonical CPython 3.13 arm64 packaging interpreter, the official OpenAI `tunnel-client`, and an explicit non-ad-hoc signing identity.

Apple's Command Line Tools guidance is:
https://developer.apple.com/documentation/xcode/installing-the-command-line-tools

The preflight never installs/selects developer tools and never enumerates Keychain identities. Maintainers explicitly supply the local packaging identity:

```bash
export AGENT_RUNTIME_CODESIGN_IDENTITY="<explicit-non-ad-hoc-signing-identity>"
```

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

Distribution packaging is an explicit opt-in release-authority mode; the default `macos/package_app.sh` behavior remains the local/source package lane.

A release authority invokes the distribution lane with both an explicit Developer ID Application identity and an explicit existing notarytool keychain-profile locator:

```bash
./macos/package_app.sh \
  --distribution \
  --signing-identity "Developer ID Application: ..." \
  --notary-keychain-profile "<existing-notarytool-profile>"
```

The distribution lane fails closed if either authority input is absent or malformed. It does not enumerate Keychain identities, create credentials, or place notary credentials in Runtime configuration or candidate metadata.

Its mutation order is fixed:

```text
Developer ID + hardened runtime + timestamp signing
  -> strict codesign verification
  -> notarization submission
  -> stapling
  -> strict codesign + Developer ID/hardened-runtime + Gatekeeper + stapler + embedded provenance verification
  -> final candidate seal
  -> candidate publication
```

Stapling occurs before candidate sealing because it changes the app being distributed. The external handoff therefore binds the final post-staple candidate, not a pre-notarization app.

## Installed-authority operation

The installed Runtime remains package-owned under `~/Applications/Agent Runtime.app`. Canonical `runtime.env` is retained by default during uninstall. Credentials and Git identity values must never be printed or logged.

The official `tunnel-client` continues to own the outbound HTTPS connection to OpenAI and the protected loopback listener at `127.0.0.1:8080`. The release installer does not register arbitrary tunnels or broaden that identity boundary.

For recovery semantics after any pending or partial cutover, follow [RECOVERY.md](RECOVERY.md).
