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

### Provision configuration

For first install, provide the required values to the installer environment. Keep the actual values out of shell history, logs, tickets, and repository files.

```bash
export CONTROL_PLANE_API_KEY="<provisioned>"
export CONTROL_PLANE_TUNNEL_ID="<provisioned>"
export AGENT_RUNTIME_GIT_NAME="<runtime-git-name>"
export AGENT_RUNTIME_GIT_EMAIL="<runtime-git-email>"
```

Optional bounded Runtime settings may also be provided with their existing names. The installer writes first-install configuration atomically to:

```text
~/Library/Application Support/Agent Runtime/runtime.env
```

The file is mode `0600`. If a canonical file already exists, it remains authoritative: the installer validates it and the explicit workspace instead of overwriting it from process environment values.

### Install the extracted release

From the extracted bundle directory:

```bash
RELEASE_INSTALLER="./Agent Runtime.app/Contents/Resources/runtime/macos/install_release.sh"
"$RELEASE_INSTALLER" --workspace-root /absolute/existing/workspace
```

The prebuilt path performs these boundaries in order:

1. require macOS arm64, the exact release bundle shape, ordinary lifecycle tools, the official tunnel client, and the accepted tunnel identity;
2. validate the app against the external candidate handoff and Gatekeeper;
3. validate or atomically initialize canonical mode-`0600` `runtime.env` without printing secrets;
4. validate the candidate again with package-owned provenance code;
5. enter the existing `candidate_cutover.py` transaction using the app's embedded Python.

The installer never builds or re-signs the app. Successful installation remains pending; it never commits automatically.

If macOS requires Background Activity approval, complete that human/platform action and then use the existing resume/recovery command from the same installer:

```bash
"$RELEASE_INSTALLER" --resume-cutover
```

After downstream acceptance, explicitly choose:

```bash
"$RELEASE_INSTALLER" --commit-cutover
# or
"$RELEASE_INSTALLER" --rollback-cutover
```

For a partial transaction, use:

```bash
"$RELEASE_INSTALLER" --recover-partial-cutover
```

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
