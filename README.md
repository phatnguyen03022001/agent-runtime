# agent-runtime

Agent Runtime is a **bounded local execution provider** for ChatGPT and other MCP clients on macOS. **MCP is the protocol**; the admitted product transport is OpenAI Secure MCP Tunnel, which keeps the Runtime private and uses outbound HTTPS rather than a public inbound listener.

The qualified Runtime is version **0.2.0** with **exactly nineteen public tools**. The native app owns the installed lifecycle through **app-owned ServiceManagement**. The older LaunchAgent model is a **migration/rollback predecessor only**.

## Supported product shape

- macOS on Apple silicon.
- Private OpenAI Secure MCP Tunnel transport through the official `tunnel-client`.
- Package-owned Runtime bytes under `~/Applications/Agent Runtime.app`.
- Canonical operator configuration at `~/Library/Application Support/Agent Runtime/runtime.env`, mode `0600`.
- One protected singleton tunnel/listener on `127.0.0.1:8080`.
- Runtime version 0.2.0, ToolContract Kernel v1, nineteen-tool public MCP contract.
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

`AGENT_RUNTIME_MAX_ACTIVE_SESSIONS` accepts 1 through 6. Inspect the effective value with `./start.sh session-limit`.

`AGENT_RUNTIME_MAX_PARALLELISM` accepts 1 through 10. Runtime admission still enforces its frozen safe ceiling. `capacity_observer` is advisory only.

Canonical `runtime.env` is retained by default during uninstall. Its credentials and Git identity values must never be printed or logged.

## Public tool surface

The Runtime exposes exactly nineteen public tools: `terminal_exec`, `terminal_start`, `terminal_poll`, `terminal_control`, `terminal_resize`, `capacity_observer`, `fs_read_batch`, `fs_list`, `fs_search`, `fs_patch`, `fs_write`, `repo_observer`, `repo_diff`, `repo_stage`, `repo_commit`, `repo_fast_forward`, `repo_publish`, `screen_capture`, and `runtime_capabilities`.

`repo_observer` provides typed local-only read-only Git observation. `repo_fast_forward` provides expected-state-guarded fixed-origin synchronization. `repo_publish` provides expected-state-guarded fixed-origin publication; repository/task authority remains outside Runtime.

`screen_capture` remains contract-visible but governance-blocked as `VISUAL_PERCEPTION_BLOCKED`. It does not request Screen Recording permission. The dormant media contract retains deterministic `cg_global_points` metadata semantics for any separately authorized future implementation.

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
