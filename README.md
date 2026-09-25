# agent-runtime

Agent Runtime is a **bounded local execution provider** for ChatGPT and other MCP clients on macOS. **MCP is the protocol**; the admitted product transport is OpenAI Secure MCP Tunnel, which keeps the Runtime private and uses outbound HTTPS rather than a public inbound listener.

The qualified Runtime source is version **0.5.0** with **exactly twenty public tools** and **twenty-one known capabilities**. The installed Runtime remains **0.4.0** until a separately reviewed activation task. The native app owns the installed lifecycle through **app-owned ServiceManagement**. The older LaunchAgent model is a **migration/rollback predecessor only**.

## Supported product shape

- macOS on Apple silicon.
- Private OpenAI Secure MCP Tunnel transport through the official `tunnel-client`.
- Package-owned Runtime bytes under `~/Applications/Agent Runtime.app`.
- Canonical operator configuration at `~/Library/Application Support/Agent Runtime/runtime.env`, mode `0600`.
- One protected singleton tunnel/listener on `127.0.0.1:8080`.
- Runtime source version 0.5.0, ToolContract Kernel v2, twenty advertised MCP tools from twenty-one known capabilities; the installed Runtime remains 0.4.0 until a separately reviewed activation task, and installation remains an explicit pinned transactional cutover rather than a side effect of source publication.
- Runtime requires no blanket TCC permissions. Background Activity approval is operator/platform state.
- Homebrew is optional; it is not an architecture prerequisite.

ServiceManagement may report `enabled`, `requires-approval`, `not-registered`, or `not-found`. Approval is a human/platform boundary; the Runtime does not open System Settings or bypass it.

## Quick Start

1. Clone the canonical repository and enter its root.
2. Create checkout configuration:

   ```bash
   cp .env.example .env
   chmod 600 .env
   ```

   Fill `CONTROL_PLANE_API_KEY`, `CONTROL_PLANE_TUNNEL_ID`, and the Runtime Git identity pair. Do not commit `.env`.

3. Supply an explicit non-ad-hoc signing identity:

   ```bash
   export AGENT_RUNTIME_CODESIGN_IDENTITY="Developer ID Application: ..."
   ```

4. Run the strict read-only preflight:

   ```bash
   ./install.sh --check
   ./install.sh --check --json
   ```

5. When preflight is `READY`, install through the canonical path:

   ```bash
   ./install.sh
   ```

6. If macOS reports Background Activity approval is required, approve it manually, then follow the recovery guidance. Start and diagnose with:

   ```bash
   ./start.sh start
   ./start.sh status
   ./start.sh doctor
   ./start.sh doctor --json
   ```

7. After downstream acceptance, explicitly commit or roll back the pending cutover:

   ```bash
   ./install.sh --commit-cutover
   # or
   ./install.sh --rollback-cutover
   ```

Detailed operator guidance:

- [Installation](docs/INSTALL.md)
- [Operations](docs/OPERATIONS.md)
- [Recovery](docs/RECOVERY.md)
- [Agent procedure](docs/AGENT.md)
- [Threat model](THREAT_MODEL.md)

## Configuration

`.env.example` is the single checkout template. OpenAI transport settings are separate from Runtime settings. The installed product reads only canonical `runtime.env`; the checkout is not an installed credential dependency.

`AGENT_RUNTIME_MAX_ACTIVE_SESSIONS` accepts 1 through 6. Inspect the effective value with `./start.sh session-limit`. Persistent terminal sessions have a fixed 3600-second running hard wall; client polling does not extend it. Completed terminal results are retained for up to 3600 seconds and at most 16 completed sessions.

`AGENT_RUNTIME_MAX_PARALLELISM` accepts 1 through 10. Runtime admission still enforces its frozen safe ceiling. `capacity_observer` result schema v2 preserves the host-capacity fields and adds point-in-time `active_heavy`, `available_heavy`, `active_sessions`, `recommended_additional_parallelism`, `observed_at`, and `reservation_guaranteed=false`; the result is advisory only and does not reserve capacity.

`AGENT_RUNTIME_TELEMETRY` accepts only `off` or `otlp` and defaults to `off`. Disabled mode does not initialize an OpenTelemetry SDK provider, exporter thread, network client, or collector connection. `otlp` mode manually exports only bounded traces and metrics over OTLP/HTTP to the fixed loopback base endpoint `http://127.0.0.1:4318`; exporter batching and timeouts are source-fixed and fail open relative to Runtime behavior. The Runtime does not accept telemetry endpoint, header, authentication, service-name, resource-label, or arbitrary user-label configuration.

Telemetry reuses the existing timing lifecycle and exports only fixed low-cardinality attributes: `service.name`, `runtime.version`, validated `runtime.revision` when available, allowlisted `tool.name`, `outcome`, `process.kind`, and `termination.state`. `runtime_call_id` is trace correlation only and is never a metric attribute. Commands, argv, paths, repository URLs, tool arguments/results, stdout/stderr, exception messages, environment values, Git identity, credentials, request IDs, session/start identities, and continuation receipts are not telemetry attributes. OpenTelemetry logs and automatic instrumentation are not enabled.

The supported topology is intentionally external and optional:

```text
Agent Runtime -> optional loopback OTLP collector -> optional Prometheus/Grafana or other backend
```

This repository does not bundle or operate the collector, Prometheus, Grafana, or another telemetry backend, and collector availability is not a health/readiness dependency.

Canonical `runtime.env` is retained by default during uninstall. Its credentials and Git identity values must never be printed or logged.

## Public tool surface

The Runtime advertises exactly twenty public tools: `terminal_exec`, `terminal_start`, `terminal_poll`, `terminal_control`, `terminal_resize`, `capacity_observer`, `fs_read_batch`, `fs_list`, `fs_search`, `fs_patch`, `fs_write`, `fs_manage`, `repo_observer`, `repo_remote_observer`, `repo_diff`, `repo_stage`, `repo_commit`, `repo_fast_forward`, `repo_publish`, and `runtime_capabilities`. The static registry retains twenty-one known capabilities. Installed activation is bound to an exact pinned package revision rather than inferred from checkout state.

Checkout source keeps PTY and pipe launches in one keyed process lifecycle. `terminal_start` defaults to PTY and can select separate stdout/stderr pipes; `terminal_exec` is a bounded synchronous facade over that pipe lifecycle. Source changes do not activate themselves: only an exact pinned package cutover changes the installed Runtime. See [source execution recovery](docs/RECOVERY.md#source-execution-recovery) before repeating a mutation after an unknown result.

Source `terminal_poll` request schema v4 keeps result schema v3, defaults to incremental output, and accepts `max_output_bytes` from 0 through 16 KiB. `output: none` returns status and lifecycle without consuming unread output; its `next_cursor` stays at the requested cursor or retained base when the requested cursor is valid; a cursor ahead of available output is rejected. A later incremental poll can read the same bytes. Poll budgets count raw bytes and do not exceed the existing 16 KiB hard limit. Valid UTF-8 code points stay intact across budget and pipe-read boundaries; when the next code point cannot fit, the cursor stays before it and the caller needs a larger budget. `wait_for` keeps its existing wait behavior.

Source `fs_read_batch` keeps request schema v1 and uses result schema v2. Successful items retain the requested text range and add `size_bytes`, `returned_bytes`, `eof`, `truncated`, and a full raw-file `sha256` when the existing scan ceilings permit proving the whole file; a completed range remains successful with `sha256=null` when the remaining full-file hash cannot fit those ceilings. That digest is directly usable as the existing `fs_patch` expected-state CAS token.

`fs_manage` uses request/result schema v1/v1 for bounded workspace-local `mkdir`, `move`, `delete`, and `chmod`. It rejects absolute/traversal/symlink paths and protected Runtime paths; move is same-filesystem atomic no-overwrite with no copy/delete fallback, delete is single-file or empty-directory only, and chmod accepts regular files with an expected SHA-256 and ordinary `0o000..0o777` permission bits only. Source changes do not activate the installed Runtime without a separately authorized cutover.

Source request/result schemas for `fs_list`, `fs_search`, `repo_diff`, and `repo_observer` are v2/v2. Initial calls omit both `cursor` and `continuation_receipt`; resume calls provide both. Successful results retain existing fields and add `truncated`, `next_cursor`, and a closed ReceiptV1-shaped `continuation_receipt`. Cursors are opaque, stateless, ASCII, bounded to 1024 characters, expire after 300 seconds, bind the tool, semantic request parameters, page position, and observed-state receipt, and are evidence only—not authorization. `fs_list` revalidates the complete bounded directory observation; `fs_search` explicitly uses `continuation_consistency=revalidated` with no snapshot/cache/store; `repo_diff` reuses the full raw-diff ReceiptV1 and pages on valid UTF-8 boundaries; `repo_observer` revalidates one exact local observation and does not manufacture a resumable cursor when an existing hard observation bound prevents exact state identity.

`repo_observer` remains typed local-only read-only Git observation with `fetched=false` and `network_used=false`. `repo_remote_observer` is a separate bounded read-only network authority: it enumerates exact `origin` branch refs without fetch or local mutation, reports current-branch absence as success, and keeps `ahead`/`behind` null because no commit graph is fetched. `repo_fast_forward` provides expected-state-guarded fixed-origin synchronization. `repo_publish` provides expected-state-guarded fixed-origin publication; repository/task authority remains outside Runtime.

`runtime_capabilities` request/result schema v2 defaults to a compact `detail=summary` response with Runtime identity, global known/advertised/availability counts, and fixed execution ceilings. `detail=full` returns the static known descriptors; an optional unique exact `names` filter is valid only with full detail and preserves registry order. Source/development summary reports `runtime_revision=null` rather than synthesizing mutable Git identity or performing network/package probes.

`screen_capture` remains a known descriptor in full capability discovery with `supported=true`, `available=false`, `advertised=false`, and `VISUAL_PERCEPTION_BLOCKED`, but it is not registered in the stable MCP `tools/list` surface and direct MCP name calls cannot reach native capture. Its implementation and packaged helper remain retained. It does not request Screen Recording permission. The dormant media contract retains deterministic `cg_global_points` metadata semantics for any separately authorized future implementation.

## Lifecycle and authority

The product boundaries are **build, package, candidate freeze, install/cutover, activation, update, rollback, uninstall, and cleanup**. One boundary does not silently authorize the next.

Normal operator commands are:

```bash
./start.sh start
./start.sh stop
./start.sh restart
./start.sh status
./start.sh session-limit
./start.sh doctor [--json]
```

The internal service entrypoint is not an operator command.

Foreign or ambiguous ownership of protected port 8080 fails closed. The Runtime never treats occupancy alone as permission to kill or rebind another process.

Use `./install.sh --uninstall` for owner-safe removal. Canonical runtime.env is retained by default.

## Security boundary

The protected Runtime filter recognizes a bounded set of lifecycle/process intents. It is **not a sandbox** and is not complete same UID isolation. A process running as the operator's **same UID** may perform effects outside the classifier's recognized forms. No allow result grants lifecycle authority.

The threat model explicitly excludes `root/sudo`, a **malicious local administrator**, kernel compromise, and equivalent higher-privilege control. See [THREAT_MODEL.md](THREAT_MODEL.md).

Production `screen_capture` is governance-blocked as `VISUAL_PERCEPTION_BLOCKED`; this is not a missing Screen Recording permission and must not be “fixed” by widening TCC permissions.

## Native build prerequisites

Packaging requires the canonical **CPython 3.13** arm64 interpreter and an explicit `AGENT_RUNTIME_CODESIGN_IDENTITY`. Apple developer tools must provide `xcrun` and Swift. See [docs/INSTALL.md](docs/INSTALL.md) for the current prerequisite boundary.
