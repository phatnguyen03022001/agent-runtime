# Operations

This document owns normal installed-product operation.

## Lifecycle

```bash
./start.sh start
./start.sh stop
./start.sh restart
./start.sh status
./start.sh session-limit
```

After installation, source-checkout lifecycle commands delegate to the package-owned helper. The internal service entrypoint is not a normal operator command. Current ownership is intentionally split by responsibility: `SMAppService.mainApp` restores the menu-bar app at user login, while `~/Library/LaunchAgents/com.picmao.agent-runtime-runtime-service.plist` is the explicit Runtime invocation/ownership mechanism. The Runtime LaunchAgent is `RunAtLoad=false` and `KeepAlive=false`; it may remain loaded while idle. A current Runtime `SMAppService.agent` registration is contradictory ownership, not redundancy.

Runtime execution is manual and session-scoped:

- `start` explicitly launches one owned Runtime generation and waits for canonical readiness.
- `stop` signals only positively proven canonical lifecycle ownership, waits for listener/supervisor absence, and is a safe no-op when already stopped.
- `restart` is one bounded explicit Stop plus Start operation.
- `status` reports current serving/control truth and effective persistent-session capacity; it carries no persistent desired-state intent.

The menu-bar app may start at user login, but Runtime does not start because of login/reboot and does not automatically restart after an unexpected child exit. A leftover pre-task `protected-runtime-running` marker is not current availability truth; it is relevant only to bounded predecessor rollback/recovery compatibility.

Foreign or ambiguous ownership of `127.0.0.1:8080` fails closed. Do not kill/rebind unknown owners.

## Doctor

```bash
./start.sh doctor
./start.sh doctor --json
```

This is the operator diagnostic authority. It uses installed package bytes plus canonical `runtime.env`; it does not repair state.

Direct `python -m agent_runtime.doctor` is a source/development diagnostic and is not the installed-product operator entrypoint.

## OpenAI tunnel health

The product transport is OpenAI Secure MCP Tunnel. `tunnel-client` connects outbound to OpenAI and keeps the Runtime private. Ordinary readiness requires exactly one canonical loopback listener and green:

```text
http://127.0.0.1:8080/healthz
http://127.0.0.1:8080/readyz
```

Do not expose the local health/admin surface publicly.

## Installed paths

```text
~/Applications/Agent Runtime.app
~/Applications/Agent Runtime.app/Contents/Resources/runtime/
~/Library/Application Support/Agent Runtime/runtime.env
~/Library/Application Support/Agent Runtime/cutover-transaction/
```

Canonical `runtime.env` is operator-owned configuration and must stay mode `0600`.

## Update

Run preflight before a new package transaction:

```bash
./install.sh --check
./install.sh
```

Do not begin a second update while a cutover transaction is present. After acceptance, choose exactly one:

```bash
./install.sh --commit-cutover
./install.sh --rollback-cutover
```

## Uninstall

```bash
./install.sh --uninstall
```

Uninstall removes only attributable product-owned state and retains canonical `runtime.env` by default. It does not reset TCC, purge unrelated Login Items, delete credentials, or clean unrelated processes/files.
