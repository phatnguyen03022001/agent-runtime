# agent-runtime

`agent-runtime` is an optional local accelerator for ChatGPT engineering work
on macOS. The Python MCP runtime remains the semantic core; the native
menu-bar app is the normal intentional lifecycle authority for one protected singleton local service. GitHub/task governance remains outside Runtime.

## Installed product and tunnel authority

Run `./install.sh` from the canonical checkout to build and install
`~/Applications/Agent Runtime.app`. Installation stages and validates an
owned app bundle before activation, then registers the menu-bar LaunchAgent in
the current login session. No logout, reboot, or next login is required.
Repeated installation refreshes one registration/process and never changes the
persisted Runtime desired state.

The installed Runtime executes package-owned bytes under:

```text
~/Applications/Agent Runtime.app/Contents/Resources/runtime/
```

That payload contains the lifecycle helper, `agent_runtime` package, and the
Runtime Python environment. The Runtime LaunchAgent and menu-bar lifecycle
backend point into this installed payload. The checkout is not an installed
implementation or credential dependency. The installed product reads the
operator-owned canonical configuration at:

```text
~/Library/Application Support/Agent Runtime/runtime.env
```

The canonical file must be a regular file with mode `0600`. It is authoritative
for installed execution and is never replaced from the checkout after
initialization. During future installation, an absent canonical file may be
initialized atomically from the checkout `.env`; the checkout file remains
available only as source development/bootstrap input.

`CONTROL_PLANE_API_KEY`, `CONTROL_PLANE_TUNNEL_ID`,
`AGENT_RUNTIME_WORKSPACE_ROOT`, and optional `AGENT_RUNTIME_MAX_PARALLELISM`
are read from the canonical file. `AGENT_RUNTIME_MAX_PARALLELISM` accepts only
integers from `1` through `10`; when absent its effective default is `2`. Invalid
values fail configuration validation instead of being silently clamped. The
accepted tunnel fingerprint is `6aa2b81d6dd8`. Never print the complete tunnel
ID or API key.
The historical `~/.config/tunnel-client/agent-runtime.yaml` profile must stay
absent; installation, startup, and recovery fail closed if it reappears.
Official `tunnel-client` and macOS launchd/system utilities are the only
external Runtime dependencies.

## Lifecycle

Run `./start.sh start|stop|restart|status` as the operator CLI. After
installation, these actions delegate to the same package-owned helper used by
the menu bar. `./start.sh --serve` is an internal launchd entry point, not a
public lifecycle API.

- **Start** sets desired state to RUNNING and succeeds only after exactly one
  canonical `tunnel-client`, one listener on `127.0.0.1:8080`, and green
  `/healthz` plus `/readyz`.
- **Stop** records STOPPED before stopping the canonical launchd job, so
  KeepAlive recovery remains suppressed until the next explicit Start.
- **Restart** performs one bounded explicit restart and returns to RUNNING.
- UI launch, login launch, polling, status refresh, installation, repository
  updates, and error handling never silently change desired state.

The Runtime LaunchAgent uses `KeepAlive` keyed to the desired-state marker.
While desired state is RUNNING, accidental canonical Runtime death is
recovered automatically without changing tunnel identity or creating a second
instance. Concurrent operator starts serialize through one lifecycle lock and
converge on the same launchd service. Foreign or ambiguous ownership of
`127.0.0.1:8080` fails closed; occupancy alone never authorizes termination.

Agent Runtime terminal tools deny clear attempts to signal, stop, relaunch,
replace, or rebind the protected Runtime, tunnel, service, or port. Denials
return `PROTECTED_RUNTIME` and append only a bounded category/tool/timestamp
audit. The menu bar shows the blocked count and last category. This is
defense-in-depth for normal unprivileged Agent Runtime use; it does not claim
to defeat `root/sudo`, a malicious local administrator, kernel compromise, or
out-of-band tools outside Agent Runtime's enforcement boundary.

## Tool surface

The MCP server exposes exactly six public tools:

- `terminal_exec(argv, cwd, timeout_seconds=300)` executes one literal argv
  with `shell=False`, disconnected stdin, bounded output, and bounded cleanup.
  `argv` is limited to 128 items, 16 KiB UTF-8 bytes per item, and 256 KiB
  aggregate UTF-8 content.
- `terminal_start(argv, cwd)` starts one literal argv in a PTY-backed process
  group and returns promptly with a session id and bounded initial output; it
  uses the same fixed `argv` limits as `terminal_exec`.
- `terminal_poll(session_id, cursor=0, wait_ms=0)` returns bounded incremental
  PTY output and current status. `session_id` is limited to 128 characters and
  `wait_ms` is bounded to 1000 ms.
- `terminal_control(session_id, action, data=None, rows=None, cols=None)`
  supports exactly `write`, `interrupt`, `terminate`, and `resize`; `session_id`
  is limited to 128 characters and write data to 64 KiB UTF-8 bytes.

- `capacity_observer()` returns one read-only, on-demand, stateless capacity
  snapshot with a bounded signal summary, reason codes, and advisory
  `capacity_parallelism_ceiling`. It does not spawn, schedule, queue, reorder,
  retry, or cancel work.
- `fs_read_batch(cwd, items)` reads 1..20 ordered cwd-relative UTF-8 regular
  files or inclusive line ranges. It rejects absolute/dot/dot-dot/empty path
  components and symlinks, never truncates successful text, and enforces fixed
  128 KiB per-item plus 256 KiB aggregate UTF-8 output ceilings, 1 MiB actual
  FD-read bytes per item, and 4 MiB actual FD-read bytes per batch. Scan-limit
  failures return no partial text; after batch scan exhaustion, remaining items
  fail without file I/O. Line indexes are limited to 2147483647. It is read-only
  and exposes no caller limit knobs.

Capacity Observer v1 reports only an x1/x2 host-capacity ceiling. The effective
ceiling is `min(AGENT_RUNTIME_MAX_PARALLELISM, evidence_based_ceiling_v1)`, so an
operator maximum of `1` always serializes, `2` permits x1/x2, and values `3`
through `10` still cannot raise v1 above x2. Architect/Executor remains
responsible for proving semantic independence before using any parallelism.
The observer uses only aggregate public macOS CPU/load, VM/swap, thermal, and
workspace-filesystem capacity signals; probe failures and critical unknowns
conservatively return x1. It keeps no telemetry history and performs no global
process inventory.

Persistent session state is memory-only. The operator-configurable positive
integer `AGENT_RUNTIME_MAX_ACTIVE_SESSIONS` controls active PTY capacity. Its
documented safe fallback is `64` when unset, malformed, or non-positive; there
is no replacement low fixed cap. Inspect the effective value with:

```bash
./start.sh session-limit
```

Changing this setting does not rotate the tunnel or restart Runtime by itself;
the new value applies on the next explicit operator Start or Restart.
Lifecycle serialization is separate from ordinary terminal execution, so
independent `terminal_exec` and `terminal_start` workloads do not share the
lifecycle lock.

`AGENT_RUNTIME_WORKSPACE_ROOT` selects the allowed working-directory tree. It
is not mechanical filesystem confinement: executable arguments retain the
operator account's normal host permissions.
`fs_read_batch` additionally anchors each item below its validated cwd with
descriptor-relative no-symlink traversal. That bounded read rule does not turn
the workspace root into general host filesystem confinement.

## Native app development and packaging

Native tests and release packaging do not start the live Runtime:

```bash
xcrun swift test --package-path macos
xcrun swift build --package-path macos -c release
./macos/package_app.sh
```

The package script verifies the menu-bar-only bundle, copies the implementation
payload into the app, writes an ownership/version manifest without checkout
paths or credential pointers, and ad-hoc signs the bundle. Installer activation
is staged, validated, and rolled back on activation failure.

## Verification

`./verify` runs deterministic Python unit/integration checks, Python compile,
and shell syntax checks. It does not require live control-plane credentials or
the live ChatGPT plugin. Native Swift tests, release build, package/signature
verification, source-checkout reference scans, installer idempotence, launchd
fixture recovery, protected-runtime checks, and bounded live acceptance are
run before publication.
