# agent-runtime

`agent-runtime` is an optional local accelerator for ChatGPT engineering work
on macOS. The Python MCP runtime remains the semantic core; the native
menu-bar app is the normal intentional lifecycle authority for one protected singleton local service. GitHub/task governance remains outside Runtime.

## Installed product and tunnel authority

Run `./install.sh` from the canonical checkout to build one sealed candidate
and enter a transactional cutover for `~/Applications/Agent Runtime.app`.
The cutover validates the sealed candidate through staging and installed
placement, refreshes only the owned LaunchAgents, preserves the Runtime desired
state, and leaves the previous known-good package/LaunchAgent state recoverable
in a pending transaction. A second cutover is rejected while one is pending.

After downstream live acceptance, commit the pending cutover explicitly with
`./install.sh --commit-cutover`. To restore the previous package and relevant
LaunchAgent/desired-state facts instead, use `./install.sh --rollback-cutover`.
An already sealed bundle can be handed off without rebuilding or re-signing via
`./install.sh --install-prebuilt <Agent Runtime.app> <candidate.json>`; this path
validates candidate-owned embedded provenance plus the caller-supplied external
candidate identity and does not use the invoking checkout HEAD as candidate
identity. The prebuilt cutover does not modify canonical `runtime.env`.

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
initialization. During first bootstrap only, an absent canonical file is
initialized atomically from the checkout `.env`: non-empty checkout credentials
win, while `CONTROL_PLANE_API_KEY` and `CONTROL_PLANE_TUNNEL_ID` from the
installer process environment fill only missing/empty checkout values. A
checkout `.env` created from `.env.example` is made mode `0600` before bootstrap.
Once canonical `runtime.env` exists, process-environment fallback never overrides
it. The checkout file remains only source development/bootstrap input.

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

Agent Runtime terminal tools apply a defense-in-depth recognized intent filter
to direct argv plus the supported shell/wrapper forms they understand. Clear
attempts to signal, stop, relaunch, replace, or rebind the protected Runtime,
tunnel, service, or port return `PROTECTED_RUNTIME` and append only a bounded
category/tool/timestamp audit. The menu bar shows the blocked count and last
category.

This filter is not a sandbox, privilege boundary, syscall filter, filesystem
confinement mechanism, or complete same UID containment. An otherwise allowed
program running as the operator's same UID can perform host effects whose
semantics are not visible to this argv/text classifier. An allow result therefore
does not grant Runtime lifecycle authority. Executor governance separately
requires exact authorization for shared Runtime lifecycle mutation. See
[`THREAT_MODEL.md`](THREAT_MODEL.md) for the covered intents, limitations, and
explicit non-goals, including `root/sudo`, a malicious local administrator,
kernel compromise, and out-of-band tools.

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

Release packaging fails closed on a dirty Git checkout, exports exact `HEAD`
into a temporary immutable source staging tree, and builds the app only from
that staged source. Python runtime dependencies come from checked-in
`requirements.lock` through `pip --require-hashes` into a fresh package-owned
virtual environment; checkout `.venv` contents are never copied into the app.

Candidate construction uses one canonical packaging interpreter identity:
CPython 3.13.x, `cp313`, macOS, arm64. Packaging and the normal install path
resolve the versioned `python3.13` executable and validate that identity before
any dependency install. If the versioned executable is intentionally outside
`PATH`, set `AGENT_RUNTIME_PACKAGING_PYTHON` to its absolute path; the override
is validated identically and does not make that machine-specific path package
authority. A generic `python3` is never an implicit packaging fallback, and an
existing checkout `.venv` must satisfy the same identity before it can install
the lock.

`requirements.txt` is the direct resolution input. `requirements.lock` is the
complete selected-artifact authorization for that single canonical target. To
regenerate or audit it, use the canonical interpreter, resolve the complete
closure from `requirements.txt` with `pip download --only-binary=:all:`, read
Name/Version from each selected wheel's `METADATA`, hash those exact wheel
bytes with SHA-256, and emit one exact `name==version --hash=sha256:<digest>`
line per selected distribution sorted by normalized project name. Then create
a fresh `--without-pip` venv and install the generated lock with
`pip --require-hashes`; the installed Name/Version set must equal the lock set
exactly. A version-set change is a dependency-contract change and must not be
silently accepted as a hash refresh. This procedure is intentionally
single-target; it does not generate a multi-platform wheel matrix. For a
hash-only audit, use the exact versions already in `requirements.lock` as pip
constraints while resolving `requirements.txt`, require the selected closure
to equal the lock set, and compare every selected wheel hash. An unconstrained
regeneration that changes any selected version must stop for dependency-change
authority before replacing the checked-in lock.

`Contents/Resources/runtime-manifest.json` records the exact Git revision and
tree, the `requirements.lock` SHA-256, every regular file below
`Contents/Resources/runtime` with path/size/SHA-256, and an aggregate digest
over the sorted canonical file list. After all candidate-mutating build work,
signing, strict codesign verification, and final runtime-manifest validation,
packaging writes the external `build/Agent Runtime.candidate.json` handoff.
Its candidate digest closes over every regular file in the logical app bundle,
including signing-owned files, using sorted UTF-8 relative path, four-digit
permission mode, byte size, and SHA-256 records. Symlinks and unsupported
non-regular entries are rejected. Staging and installed placement must preserve
that exact external closure as well as strict codesign and embedded manifest
closure. This is reconstructible provenance, not a claim of bit-for-bit
reproducible builds.

## Verification

`./verify` runs deterministic Python unit/integration checks, Python compile,
and shell syntax checks. It does not require live control-plane credentials or
the live ChatGPT plugin. Native Swift tests, release build, package/signature
verification, source-checkout reference scans, pending-cutover/rollback fixtures, launchd
fixture recovery, protected-runtime checks, and bounded live acceptance are
run before publication.
