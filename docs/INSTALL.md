# Installation

This document owns the supported fresh-macOS installation path. It does not redefine Runtime architecture or recovery semantics.

## Prerequisites

Agent Runtime packaging is qualified for macOS on Apple silicon. The installer requires Git, `launchctl`, `lsof`, `curl`, `xcrun`, Swift, the canonical CPython 3.13 arm64 packaging interpreter, the official OpenAI `tunnel-client`, and an explicit non-ad-hoc signing identity.

Apple's current Command Line Tools guidance is:
https://developer.apple.com/documentation/xcode/installing-the-command-line-tools

OpenAI Secure MCP Tunnel guidance and `tunnel-client` acquisition are:
https://developers.openai.com/api/docs/guides/secure-mcp-tunnels

The preflight detects missing developer tools but does not install or select them. It never enumerates Keychain identities. If signing is required, the operator must set:

```bash
export AGENT_RUNTIME_CODESIGN_IDENTITY="Developer ID Application: ..."
```

## 1. Read-only preflight

Run before any install mutation:

```bash
./install.sh --check
./install.sh --check --json
```

The preflight is read-only. It does not create or modify `.venv`, install packages, create configuration, build/sign/package an app, mutate Git state, change ServiceManagement, change desired state, create cutover state, touch TCC, open System Settings, or enumerate Keychain identities.

Each non-pass check has a stable `reason_code` and one action class:

- `SAFE_AUTOMATED`
- `HUMAN_ACTION_REQUIRED`
- `STOP_AND_ESCALATE`

Do not run the mutating installer while preflight status is `BLOCKED`.

## 2. Configuration

There is one checkout template: `.env.example`.

```bash
cp .env.example .env
chmod 600 .env
```

OpenAI transport settings:

```text
CONTROL_PLANE_API_KEY=
CONTROL_PLANE_TUNNEL_ID=
```

Runtime settings:

```text
AGENT_RUNTIME_WORKSPACE_ROOT=
AGENT_RUNTIME_GIT_NAME=
AGENT_RUNTIME_GIT_EMAIL=
AGENT_RUNTIME_MAX_ACTIVE_SESSIONS=6
AGENT_RUNTIME_MAX_PARALLELISM=2
```

The installer derives the canonical workspace from the checkout parent. Runtime Git identity requires a valid name/email pair but diagnostics never print the values. On first initialization, repository-local `user.name` and `user.email` may supply the pair when explicit installer values are absent.

The installed authority is:

```text
~/Library/Application Support/Agent Runtime/runtime.env
```

It must be a regular non-symlink file with mode `0600`. First installation initializes it atomically. Once present, it remains canonical and is not overwritten from checkout credentials. Uninstall retains it unless product behavior is explicitly changed in a future reviewed release.

## 3. Install

After a `READY` preflight:

```bash
./install.sh
```

The canonical installer creates/validates the local environment, runs verification, initializes canonical configuration, validates the OpenAI tunnel configuration, builds/signs the sealed app candidate, and enters transactional cutover.

Installation may stop at a human/platform boundary. Background Activity approval cannot be bypassed or automated.

## 4. First start

After modern ServiceManagement registration is enabled:

```bash
./start.sh start
./start.sh status
```

The Runtime is private. The official `tunnel-client` owns the outbound HTTPS connection to OpenAI and the local protected listener remains loopback-only at `127.0.0.1:8080`.

## 5. Installed-authority doctor

Use the product wrapper, not source Python:

```bash
./start.sh doctor
./start.sh doctor --json
```

The wrapper delegates to package-owned Runtime bytes, safely parses canonical `runtime.env`, sets `PYTHONDONTWRITEBYTECODE=1`, and reuses `python -m agent_runtime.doctor`. DoctorReport remains schema v1 with the existing ten checks.

Missing app/config state fails deterministically. Credentials and Git identity values are never printed.

## 6. Cutover decision

Installation leaves the candidate pending until an explicit downstream decision.

Commit the accepted candidate:

```bash
./install.sh --commit-cutover
```

Roll back to the attributable predecessor:

```bash
./install.sh --rollback-cutover
```

If the transaction is incomplete, do not guess. Follow [RECOVERY.md](RECOVERY.md).
