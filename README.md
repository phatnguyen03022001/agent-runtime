# agent-runtime

Agent Runtime is a **bounded local execution provider** for ChatGPT and other MCP clients on macOS. **MCP is the protocol**; the admitted product transport is OpenAI Secure MCP Tunnel, which keeps the Runtime private and uses outbound HTTPS rather than a public inbound listener.

The installed Runtime is version **0.5.0** with **exactly twenty public tools** and **twenty-one known capabilities**. The current zero-cost source lane packages an immutable ad-hoc app substrate and a separate content-addressed first-party Python payload. One per-user LaunchAgent starts the embedded supervisor; ServiceManagement remains only for predecessor migration and rollback.

## Supported product shape

- macOS on Apple silicon.
- Private OpenAI Secure MCP Tunnel transport through the official `tunnel-client`.
- Immutable native, CPython 3.13, third-party and bootstrap substrate under `~/Applications/Agent Runtime.app`; selected first-party Python release under `~/Library/Application Support/Agent Runtime/payloads/<closure>` with one `current-payload` pointer.
- Canonical operator configuration at `~/Library/Application Support/Agent Runtime/runtime.env`, mode `0600`.
- One protected singleton tunnel/listener on `127.0.0.1:8080`.
- Runtime source and installed Runtime version 0.5.0, ToolContract Kernel v2, twenty advertised MCP tools from twenty-one known capabilities; installation remains an explicit pinned transactional cutover rather than a side effect of source publication.
- Runtime requires no blanket TCC permissions. The current traditional user LaunchAgent does not use ServiceManagement Background Activity registration.
- Homebrew is optional; it is not an architecture prerequisite.

The first ad-hoc substrate launch may require the operator to use macOS Open/Open Anyway for that exact app. Ad-hoc signing seals code integrity but does not authenticate a publisher. Ordinary validated pure-Python payload activation does not replace the app; a later native/substrate release may require approval again.

## Installation paths

### Prebuilt release — newcomer path

A qualified release bundle is checkout-independent. It contains ad-hoc sealed `Agent Runtime.app`, external `Agent Runtime.candidate.json`, and `payloads/<initial-closure>/`. Keep the complete bundle together and open `Agent Runtime.app`.

Download the [v0.5.0 zero-cost release](https://github.com/phatnguyen03022001/agent-runtime/releases/tag/v0.5.0) and `SHA256SUMS.txt`. Verify the archive with `shasum -a 256 -c SHA256SUMS.txt` before extraction. Its SHA-256 is `3f179015534d81d6baee3faf78a57f8300451739afdda53a5940ee91ec7c3746`, and the release tag identifies packaged source revision `092b761cdc7e3f1085abe38948d9613fefe9e875`.

On a fresh machine the existing app opens native setup automatically. Enter the provisioned Control Plane API key and tunnel ID in secure fields, provide the Runtime Git name/email pair when requested, and choose the workspace with the macOS folder picker. The normal newcomer path does **not** require Terminal, shell exports, manual `runtime.env` editing, or typing a workspace path.

The app delegates validation, canonical mode-`0600` configuration publication, release preflight/provenance, candidate cutover, resume/recovery, doctor, readiness, and commit to the existing package-owned helpers. It does not reimplement lifecycle semantics in Swift. A recognized pending cutover may be doctor `degraded` before commit only because of `CUTOVER_TRANSACTION_PRESENT`; any additional doctor warning or failure blocks commit. Setup reports success only after the existing commit resolves the transaction, readiness is `ready`, and package-owned doctor is `healthy`.

If macOS blocks the first ad-hoc app launch, use the platform's Open/Open Anyway control for the exact app and reopen the complete bundle. Setup then validates the external handoff and payload before cutover. The official OpenAI `tunnel-client` remains an external prerequisite and is neither installed nor upgraded by setup.

An already-valid installed canonical configuration remains authoritative and goes directly to the current control panel without being rewritten or re-prompted. No agent-runtime Git clone, CPython 3.13, Swift/Xcode, local signing identity, or notary credentials are required on the consumer Mac.

The public v0.5.0 artifact is ad-hoc signed without Apple Developer ID or notarization. macOS may require Open/Open Anyway for the exact downloaded app.

### Build from source — maintainers

The source maintainer lane uses explicit zero-cost ad-hoc packaging. Clone the canonical repository, create `.env` from `.env.example`, then run:

```bash
./install.sh --check
./install.sh --check --json
./install.sh
```

This lane uses the canonical CPython 3.13 packaging interpreter plus Xcode/Swift, builds and signs locally, and leaves the same pending transactional cutover. Start and diagnose from the checkout with:

```bash
./start.sh start
./start.sh status
./start.sh doctor
./start.sh doctor --json
```

Then explicitly commit or roll back:

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

Source `terminal_poll` uses request/result schemas v4/v4, defaults to incremental output, and accepts `max_output_bytes` from 0 through 16 KiB. `output: none` returns status and lifecycle without consuming unread output; its `next_cursor` stays at the requested cursor or retained base when the requested cursor is valid; a cursor ahead of available output is rejected. A later incremental poll can read the same bytes. Poll budgets count raw bytes and do not exceed the existing 16 KiB hard limit. Valid UTF-8 code points stay intact across budget and pipe-read boundaries; when the next code point cannot fit, the cursor stays before it and the caller needs a larger budget. `wait_for` keeps its existing wait behavior.

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

## Packaging prerequisites

Source packaging requires the canonical **CPython 3.13** arm64 interpreter and Apple developer tools with `xcrun` and Swift. `./macos/package_app.sh --zero-cost` signs the immutable substrate ad-hoc, emits the external initial payload and handoff, and validates the candidate before publication. It requires no Developer ID or notary credentials. See [docs/INSTALL.md](docs/INSTALL.md).
