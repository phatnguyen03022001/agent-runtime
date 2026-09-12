# agent-runtime

`agent-runtime` is an optional local accelerator for ChatGPT engineering work on macOS. The Python MCP runtime remains the semantic core; the native macOS menu-bar app is the normal intentional lifecycle authority for one protected singleton local service. GitHub/task governance remains outside Runtime. The lifecycle layer is deliberately small: one launchd job, one desired-state marker, one bounded protection audit, and the existing Secure MCP Tunnel path.

## Lifecycle

Run `./install.sh` to prepare the checkout-local `.venv` and `.env`, build/install `~/Applications/Agent Runtime.app`, and install the UI plus protected Runtime LaunchAgents. `.env` is the sole persistent owner of `CONTROL_PLANE_API_KEY`, `CONTROL_PLANE_TUNNEL_ID`, and `AGENT_RUNTIME_WORKSPACE_ROOT`. The historical `~/.config/tunnel-client/agent-runtime.yaml` configuration must remain absent; installation, startup, and recovery fail closed if it reappears. Re-running installation is idempotent and must not change the operator's RUNNING/STOPPED desired state as an install side effect.

Normal intentional lifecycle control is the menu-bar app:

- **Start** sets desired state to RUNNING and returns success only after launchd converges to one canonical `tunnel-client`, one loopback listener, and green `/healthz` plus `/readyz`.
- **Stop** sets desired state to STOPPED before terminating the canonical launchd job, so KeepAlive recovery stays suppressed until the next explicit Start.
- **Restart** keeps desired state RUNNING and performs one bounded restart of the canonical singleton.
- App launch, login launch, status refresh, polling, repository updates, verification, and error handling do not silently change desired state.

The Runtime LaunchAgent uses macOS launchd `KeepAlive` keyed to the desired-state marker. While desired state is RUNNING, accidental death is recovered without creating or rotating tunnel identity. Concurrent Start attempts serialize through one lifecycle lock and converge on the single launchd service rather than spawning competing tunnel/MCP instances. `./start.sh start|stop|restart|status` is the bounded operator CLI recovery path; `./start.sh --serve` is the internal launchd entry point and is not a public lifecycle API.

Port `127.0.0.1:8080` is protected, but occupancy alone is never authority to terminate a process. A foreign or ambiguous listener fails closed with an operator-visible error. Only a positively identified canonical installation instance may be managed by the bounded lifecycle path.

The canonical invocation passes the `.env` identity only through a sanitized process environment, uses the main MCP channel, runs `<checkout>/.venv/bin/python -m agent_runtime.server`, and binds health only to `127.0.0.1:8080`. Its generated LaunchAgent passes a verified absolute `tunnel-client` path and a deterministic launchd PATH, so it does not depend on an interactive shell or browser startup behavior.

Agent Runtime's own terminal execution boundary denies clear attempts to terminate, signal, stop, relaunch, replace, or rebind the protected Runtime, tunnel, service, or port. Enforcement covers direct execution, compound shell commands, supported executable wrappers, and writes into an interactive PTY shell. Denials return `PROTECTED_RUNTIME` and append only a bounded count/category/tool/timestamp audit; full command payloads and secrets are not persisted. The menu-bar UI shows the blocked count and last category.

This is defense-in-depth against accidental or concurrent Executor actions under normal unprivileged use. It does not claim protection against `root/sudo`, a malicious local administrator, kernel compromise, or destructive out-of-band tools outside Agent Runtime's enforcement boundary.

## Tool surface

The MCP server exposes exactly four public tools:

- `terminal_exec(argv, cwd, timeout_seconds=300)` is the preferred one-shot primitive. It executes literal argv with `shell=False`, disconnected stdin, bounded output capture, and bounded timeout/process-group cleanup.
- `terminal_start(argv, cwd)` starts one literal argv in a PTY-backed process group and returns promptly with a session id, current status, bounded initial output, and cursor state.
- `terminal_poll(session_id, cursor=0, wait_ms=0)` returns only bounded output newer than the requested cursor, current status, the next cursor, and an exit code after natural termination. `wait_ms` is bounded to 1000 ms for near-realtime long-polling. If retained output has already been evicted, the response reports cursor expiry and dropped byte count explicitly.
- `terminal_control(session_id, action, data=None, rows=None, cols=None)` supports exactly `write`, `interrupt`, `terminate`, and `resize`. `write` sends UTF-8 PTY input through the same protected-runtime guard before delivery, `interrupt` signals only the task-owned PTY process group, `terminate` performs bounded cleanup of that PTY process group, and `resize` updates PTY rows/columns.

Persistent MCP session state is memory-only. At most three sessions may be active at once, idle sessions expire after a fixed 10 minutes, retained output and poll responses are bounded, and process groups plus PTY descriptors are reclaimed on termination, natural exit, idle expiry, and normal runtime shutdown. Agent Runtime does not persist MCP session metadata, logs, cursor state, PID registries, databases, caches, or recovery files.

`AGENT_RUNTIME_WORKSPACE_ROOT` must name an absolute existing directory. `cwd` is realpath-checked to be at or below that root. This is only a working-directory/path-selection guard: executable arguments can still access other host paths using the operator account's normal permissions. It is **not** mechanical filesystem confinement.

Child commands receive only a small ordinary execution environment (`PATH`, `HOME`, `USER`, `TMPDIR`, `LANG`, and `LC_*` when present). Control-plane/runtime variables and token/key/credential-style ambient variables are not forwarded by default, and callers cannot supply an environment override.

There is no project registry, sync primitive, verifier state engine, broad command blacklist, approval broker, external scheduler framework, remote lifecycle server, Apple Sandbox profile, container, database, queue, or orchestration layer. Runtime supervision is limited to the single repository-owned macOS launchd job; persistent terminal-session cleanup remains one small in-process reaper.

## Native app development and packaging

Run native tests and builds without touching the live tunnel:

```bash
xcrun swift test --package-path macos
xcrun swift build --package-path macos -c release
./macos/package_app.sh
```

The package script creates `build/Agent Runtime.app`, verifies `LSUIElement=true`, and ad-hoc signs the local bundle. Its resource pointer references the current checkout so the app reuses the checkout-local `start.sh`, `.env`, and `.venv` instead of duplicating configuration ownership.

## Verification

`./verify` is deterministic Python verification plus compile/shell-syntax checks. It does not require live control-plane credentials, a live tunnel, or a live ChatGPT plugin. Native Swift tests, release build, packaging, signature verification, and static contract checks run before the single bounded live lifecycle acceptance, so ordinary verification never mutates the operator's live Runtime.
