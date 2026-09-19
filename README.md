# agent-runtime

`agent-runtime` is a bounded local execution provider for macOS. MCP is the protocol
exposed by Runtime; tunnel, connection, and stdio are transport routes rather
than provider identity. Compatible MCP clients may use the provider; the
architecture is not owned by ChatGPT, Codex, Antigravity, or another client.
The native menu-bar app owns intentional lifecycle control for one protected singleton
local service. GitHub/task governance remains outside Runtime.

## Installed product and tunnel authority

Run `./install.sh` from the canonical checkout to build one sealed candidate
and enter a transactional cutover for `~/Applications/Agent Runtime.app`. The
current generation is owned by Agent Runtime.app through app-owned ServiceManagement:
`SMAppService.mainApp` owns login launch and the bundled Runtime LaunchAgent owns
the signed `AgentRuntimeRuntimeService` responsible executable. The previous
`~/Library/LaunchAgents` model is a migration/rollback predecessor only.

Cutover validates the sealed candidate through staging and installed placement,
snapshots the exact predecessor package/plist bytes, loaded states, and desired
state, removes proven legacy ownership, registers the modern services, and
rejects dual ownership. The transaction becomes pending only after modern
registration has an attributable valid state. A second cutover is rejected
while one is pending.

After downstream live acceptance, commit the pending cutover explicitly with
`./install.sh --commit-cutover`. To restore the previous package and relevant
LaunchAgent/desired-state facts instead, use `./install.sh --rollback-cutover`;
rollback first unregisters attributable modern ownership, then restores the exact
legacy predecessor package, plist bytes, loaded states, and desired-state fact.
An already sealed bundle can be handed off without rebuilding or re-signing via
`./install.sh --install-prebuilt <Agent Runtime.app> <candidate.json>`; this path
validates candidate-owned embedded provenance plus the caller-supplied external
candidate identity and does not use the invoking checkout HEAD as candidate
identity. The prebuilt cutover does not modify canonical `runtime.env`.

ServiceManagement diagnostics expose exactly `enabled`, `requires-approval`,
`not-registered`, and `not-found`. Background Activity approval is
operator/platform state, not Runtime task authority; Start/Restart fail closed
when required registration is absent or still requires approval rather than
recreating a legacy LaunchAgent.

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
shared heavy-execution admission limit is always `min(operator setting, 6)`, so
an operator value above six never expands the supported execution envelope. The
accepted tunnel fingerprint is `6aa2b81d6dd8`. Never print the complete tunnel
ID or API key.
The historical `~/.config/tunnel-client/agent-runtime.yaml` profile must stay
absent; installation, startup, and recovery fail closed if it reappears.

Prerequisites are classified by consumer: macOS/system utilities support the
installed lifecycle; the Apple developer toolchain is required for source/native
builds; the host `tunnel-client` supplies the transport; canonical CPython 3.13
is the packaging interpreter; project-local Python dependencies support source
development and verification; and packaged Runtime dependencies are bundled in
the installed app. Homebrew is optional as an acquisition mechanism, not an
architecture prerequisite.

There are no blanket TCC permissions for Runtime. Accessibility, Automation/
Apple Events, Screen & System Audio Recording, Full Disk Access, Files &
Folders, Developer Tools, Input Monitoring, and Local Network are not baseline
Runtime permissions. The read-only `screen_capture` tool requires Screen Recording
permission to have already been granted; Runtime never requests it, opens System
Settings, or mutates TCC state. Background Activity is the separate operator-controlled
ServiceManagement approval described above; denial or revocation is reported and
fails closed rather than widening permissions.

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

The app-owned Runtime LaunchAgent uses `KeepAlive` with unsuccessful-exit
recovery. Its signed responsible executable checks the desired-state marker
before launching the package-owned `start.sh --serve` path: explicit Stop
removes the marker and converges to a successful exit, while unexpected child
death during desired RUNNING exits non-zero so launchd may recover it. Concurrent
operator starts serialize through one lifecycle lock and converge on the same
service. Foreign or ambiguous ownership of `127.0.0.1:8080` fails closed;
occupancy alone never authorizes termination.

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

The MCP server exposes exactly eleven public tools:

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
- `terminal_control(session_id, action, data=None)` supports exactly `write`,
  `interrupt`, and `terminate`. It remains truthfully destructive/open-world;
  `session_id` is limited to 128 characters and write data to 64 KiB UTF-8 bytes.
- `terminal_resize(session_id, rows, cols)` performs only bounded PTY resize with
  dimensions from 1 through 65535. It is non-destructive, closed-world, and
  idempotent at the public tool-contract level.

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
- `repo_observer(cwd, max_paths=200)` provides typed local-only read-only Git observation
  with no fetch, network use, or repository mutation.
- `repo_fast_forward(cwd, branch, expected_local_head, expected_remote_head)` provides
  expected-state-guarded fixed-origin synchronization. It fresh-fetches only the
  bound branch from literal `origin` and permits only an exact fast-forward of the
  current clean branch. Runtime executes this consequence; repository/task authority
  remains outside Runtime.
- `repo_publish(cwd, branch, expected_remote_head, commit)` provides
  expected-state-guarded fixed-origin publication of exactly the current clean branch
  HEAD when `commit` is the sole direct child of the bound existing `origin/<branch>`
  head. Same-input replay freshly verifies remote state and does not push twice. Runtime
  executes the publication consequence; repository/task authority remains outside Runtime.
- `screen_capture(target="frontmost_window", ...)` captures exactly one selected window,
  application window, display, or contained display region through package-owned
  ScreenCaptureKit. Success returns one PNG `ImageContent` plus closed metadata in
  `cg_global_points`; no cursor, audio, OCR, Accessibility, clipboard, DOM, URL, or
  keystroke surface is exposed. Screen Recording permission must already be granted.

Capacity Observer v2 reports an advisory healthy-host ceiling up to x6. The
effective healthy ceiling is `min(AGENT_RUNTIME_MAX_PARALLELISM, 6)`: the
operator range remains `1` through `10`, the default remains `2`, and operator
limits below x6 remain authoritative. Existing CPU/load, thermal, swap, memory,
disk, and unknown-signal pressure gates conservatively return x1. The observer
remains advisory-only: it does not schedule, admit, queue, retry, or orchestrate
work. The independent process-local admission boundary remains authoritative
even if the observer is stale or reports a higher value. It uses only aggregate
public macOS CPU/load, VM/swap, thermal, and workspace-filesystem capacity
signals, keeps no telemetry history, and performs no global process inventory.

Persistent session state is memory-only. The operator-configurable integer
`AGENT_RUNTIME_MAX_ACTIVE_SESSIONS` controls active PTY capacity and accepts
only `1` through `6`; its safe fallback is `6` when unset or malformed. A live
PTY holds one shared heavy-execution lease for its full lifetime, and a
one-shot `terminal_exec` holds one through process-group cleanup and output
drain. Across both tool types Runtime admits at most six heavy roots; a seventh
request fails immediately with a stable capacity error before creating a
subprocess or PTY. Runtime deliberately provides no queue, delayed admission,
automatic retry, scheduler, worker pool, fairness guarantee, or per-caller
quota. Inspect the effective PTY value with:

```bash
./start.sh session-limit
```

Changing this setting does not rotate the tunnel or restart Runtime by itself;
the new value applies on the next explicit operator Start or Restart.
Lifecycle serialization is separate from ordinary terminal execution. On a
graceful Runtime `SIGTERM` or `SIGINT`, Runtime cleans the process groups it
currently owns for both in-flight one-shot executions and persistent PTYs.
This is best-effort cleanup ownership, not a job registry or recovery system;
same-UID malicious or pathological children can still escape ordinary process
management or exhaust host resources.

`AGENT_RUNTIME_WORKSPACE_ROOT` selects the allowed working-directory tree. Public
MCP schemas keep `cwd` connector-portable and do not rely on an absolute-path
regex; Runtime itself still requires a non-empty absolute cwd, resolves it
strictly, and rejects directories outside the configured workspace before
execution, PTY allocation, or batch reads. It is not mechanical filesystem
confinement: executable arguments retain the operator account's normal host
permissions.
`fs_read_batch` additionally anchors each item below its validated cwd with
descriptor-relative no-symlink traversal. That bounded read rule does not turn
the workspace root into general host filesystem confinement.

## Product operation and cleanup boundaries

The operations build, package, candidate freeze, install/cutover, activation, update, rollback, uninstall, and cleanup are distinct authority boundaries. Source packaging does not authorize live activation, and installation/cutover does not imply acceptance or transaction commit. Real service registration, Runtime restart/continuity proof, and System Settings attribution are activation work. Rollback restores a predecessor generation; uninstall removes only proven product-owned state; cleanup is neither of those.

Use `./install.sh --uninstall` for owner-safe product removal. It refuses pending
transactions or ambiguous ownership, unregisters only attributable modern
services, removes exact proven legacy remnants and bounded Runtime-owned
transient state, and removes the installed app. Canonical runtime.env is retained by default. Uninstall and
cleanup never imply TCC or Background Task Management reset, global login-item
purge, container removal, global cache sweeping, credential deletion, or
unrelated process cleanup.

## Native app development and packaging

Native tests and release packaging do not start the live Runtime:

```bash
xcrun swift test --package-path macos
xcrun swift build --package-path macos -c release
AGENT_RUNTIME_CODESIGN_IDENTITY="<explicit non-ad-hoc identity>" ./macos/package_app.sh
```

Canonical packaging requires `AGENT_RUNTIME_CODESIGN_IDENTITY` to name an
explicit non-ad-hoc signing identity. It never discovers or selects identities
from Keychain. Candidate provenance requires a non-null matching TeamIdentifier
for the main app, `AgentRuntimeRuntimeService`, and the package-owned
`AgentRuntimeScreenCapture` helper.

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
packaging seals the app and external handoff in run-owned staging, then
publishes the pair under `build/candidates/<candidate_sha256>/`. The published
`Agent Runtime.app` and `Agent Runtime.candidate.json` are sibling artifacts,
and the package/freeze result reports both exact paths plus the candidate
SHA-256. Existing candidate directories are immutable collision boundaries;
there is no authoritative mutable `latest` or singleton candidate path.

The candidate digest closes over every regular file in the logical app bundle,
including signing-owned files, using sorted UTF-8 relative path, four-digit
permission mode, byte size, and SHA-256 records. Symlinks and unsupported
non-regular entries are rejected. Staging and installed placement must preserve
that exact external closure as well as strict codesign and embedded manifest
closure. This is reconstructible provenance, not a claim of bit-for-bit
reproducible builds. Candidate immutability is prospective for newly frozen
artifacts; it does not reconstruct or restore the historical TASK-0056 bytes
that were already overwritten under the legacy singleton paths.

## Verification

`./verify` runs deterministic Python unit/integration checks, Python compile,
and shell syntax checks. It does not require live control-plane credentials or
the live ChatGPT plugin. Native Swift tests, release build, package/signature
verification, source-checkout reference scans, pending-cutover/rollback fixtures, launchd
fixture recovery, protected-runtime checks, and bounded live acceptance are
run before publication.
