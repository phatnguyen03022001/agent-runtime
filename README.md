# agent-runtime

`agent-runtime` is an optional, manually started local accelerator for ChatGPT engineering work on macOS. It is not workflow authority, a required dependency, a daemon, or a sandbox.

## Lifecycle

Run `./install.sh` once to prepare the checkout-local `.venv` and `.env` plus the existing `agent-runtime` tunnel-client profile. When local Terminal capability is useful, keep `./start.sh` running in a foreground Terminal and create/refresh the existing ChatGPT plugin exposure against that live tunnel. Stopping that foreground process makes agent-runtime unavailable; ordinary ChatGPT/GitHub Executor capability remains the normal fallback and needs no runtime-specific ceremony.

## Tool surface

The MCP server exposes exactly four public tools:

- `terminal_exec(argv, cwd, timeout_seconds=300)` is the preferred one-shot primitive. It executes literal argv with `shell=False`, disconnected stdin, bounded output capture, and bounded timeout/process-group cleanup.
- `terminal_start(argv, cwd)` starts one literal argv in a PTY-backed process group and returns promptly with a session id, current status, bounded initial output, and cursor state.
- `terminal_poll(session_id, cursor=0, wait_ms=0)` returns only bounded output newer than the requested cursor, current status, the next cursor, and an exit code after natural termination. `wait_ms` is bounded to 1000 ms for near-realtime long-polling. If retained output has already been evicted, the response reports cursor expiry and dropped byte count explicitly.
- `terminal_control(session_id, action, data=None, rows=None, cols=None)` supports exactly `write`, `interrupt`, `terminate`, and `resize`. `write` sends UTF-8 PTY input, `interrupt` sends SIGINT to the process group, `terminate` performs bounded TERM-to-KILL cleanup, and `resize` updates PTY rows/columns.

Persistent session state is memory-only. At most three sessions may be active at once, idle sessions expire after a fixed 10 minutes, retained output and poll responses are bounded, and process groups plus PTY descriptors are reclaimed on termination, natural exit, idle expiry, and normal runtime shutdown. Agent Runtime does not persist session metadata, logs, cursor state, PID registries, databases, caches, or recovery files.

`AGENT_RUNTIME_WORKSPACE_ROOT` must name an absolute existing directory. `cwd` is realpath-checked to be at or below that root. This is only a working-directory/path-selection guard: executable arguments can still access other host paths using the operator account's normal permissions. It is **not** mechanical filesystem confinement.

Child commands receive only a small ordinary execution environment (`PATH`, `HOME`, `USER`, `TMPDIR`, `LANG`, and `LC_*` when present). Control-plane/runtime variables and token/key/credential-style ambient variables are not forwarded by default, and callers cannot supply an environment override.

There is no project registry, sync primitive, verifier state engine, command allowlist, approval broker, external scheduler, autostart service, supervisor daemon, Apple Sandbox profile, container, or orchestration layer. The only persistent-session cleanup mechanism is one small in-process reaper.

## Verification

`./verify` runs deterministic tests and compile/shell-syntax checks. It does not require live control-plane credentials, a live tunnel, or a live ChatGPT plugin.
