# agent-runtime

`agent-runtime` is an optional local accelerator for ChatGPT engineering work on macOS. The Python MCP runtime remains the semantic core; the native macOS menu-bar app is only an operator lifecycle surface around the existing launch path. It is not workflow authority, a required dependency, a daemon, a sandbox, or an orchestration layer.

## Lifecycle

Run `./install.sh` once to prepare the checkout-local `.venv` and `.env`, preserve the existing `agent-runtime` tunnel-client profile, build/install `~/Applications/Agent Runtime.app`, and write a UI-only login LaunchAgent. Installation does not start, stop, restart, signal, replace, or rebind Runtime.

Normal use is the menu-bar app:

- **Start** explicitly launches the existing `./start.sh` path in an isolated process group.
- **Stop** is enabled only when the app can positively revalidate the exact process instance that it started.
- **Restart** is the same explicit owned Stop followed by Start.
- App launch, login launch, status refresh, relaunch, timers, health observation, and error handling never start or repair Runtime automatically.

The login LaunchAgent starts only `Agent Runtime.app` at the next macOS login and has `KeepAlive=false`. It never starts the tunnel or MCP runtime. `./start.sh` remains a supported foreground CLI fallback and rollback path.

If an `agent-runtime` tunnel is already running outside the app, the menu-bar UI reports it as **Running externally** and disables destructive lifecycle controls. The app never adopts, signals, kills, restarts, or reclaims that process. A listener, PID, or process-name match alone is never destructive ownership proof.

For app-owned Runtime instances, ownership is revalidated from a persisted record containing the exact PID, process-group ID, process start time, executable path, tunnel profile, and checkout root. PID reuse or stale/ambiguous metadata fails closed. No control-plane credentials are copied into Swift preferences, plist files, logs, or ownership metadata.

## Tool surface

The MCP server exposes exactly four public tools:

- `terminal_exec(argv, cwd, timeout_seconds=300)` is the preferred one-shot primitive. It executes literal argv with `shell=False`, disconnected stdin, bounded output capture, and bounded timeout/process-group cleanup.
- `terminal_start(argv, cwd)` starts one literal argv in a PTY-backed process group and returns promptly with a session id, current status, bounded initial output, and cursor state.
- `terminal_poll(session_id, cursor=0, wait_ms=0)` returns only bounded output newer than the requested cursor, current status, the next cursor, and an exit code after natural termination. `wait_ms` is bounded to 1000 ms for near-realtime long-polling. If retained output has already been evicted, the response reports cursor expiry and dropped byte count explicitly.
- `terminal_control(session_id, action, data=None, rows=None, cols=None)` supports exactly `write`, `interrupt`, `terminate`, and `resize`. `write` sends UTF-8 PTY input, `interrupt` sends SIGINT to the process group, `terminate` performs bounded TERM-to-KILL cleanup, and `resize` updates PTY rows/columns.

Persistent MCP session state is memory-only. At most three sessions may be active at once, idle sessions expire after a fixed 10 minutes, retained output and poll responses are bounded, and process groups plus PTY descriptors are reclaimed on termination, natural exit, idle expiry, and normal runtime shutdown. Agent Runtime does not persist MCP session metadata, logs, cursor state, PID registries, databases, caches, or recovery files.

`AGENT_RUNTIME_WORKSPACE_ROOT` must name an absolute existing directory. `cwd` is realpath-checked to be at or below that root. This is only a working-directory/path-selection guard: executable arguments can still access other host paths using the operator account's normal permissions. It is **not** mechanical filesystem confinement.

Child commands receive only a small ordinary execution environment (`PATH`, `HOME`, `USER`, `TMPDIR`, `LANG`, and `LC_*` when present). Control-plane/runtime variables and token/key/credential-style ambient variables are not forwarded by default, and callers cannot supply an environment override.

There is no project registry, sync primitive, verifier state engine, command allowlist, approval broker, external scheduler, autostart Runtime service, supervisor daemon, Apple Sandbox profile, container, or orchestration layer. The only persistent-session cleanup mechanism is one small in-process reaper.

## Native app development and packaging

Run native tests and builds without touching the live tunnel:

```bash
xcrun swift test --package-path macos
xcrun swift build --package-path macos -c release
./macos/package_app.sh
```

The package script creates `build/Agent Runtime.app`, verifies `LSUIElement=true`, and ad-hoc signs the local bundle. Its resource pointer references the current checkout so the app reuses the checkout-local `start.sh`, `.env`, `.venv`, and canonical tunnel profile instead of duplicating configuration ownership.

## Verification

`./verify` keeps its existing meaning: deterministic Python tests plus compile/shell-syntax checks. It does not require live control-plane credentials, a live tunnel, or a live ChatGPT plugin. Native Swift tests/build/package verification are run separately so verification never starts or stops the operator's live Runtime.
