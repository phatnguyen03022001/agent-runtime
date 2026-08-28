# agent-runtime

`agent-runtime` is an optional, manually started local accelerator for ChatGPT engineering work on macOS. It is not workflow authority, a required dependency, a daemon, or a sandbox.

## Lifecycle

Run `./install.sh` once to prepare the checkout-local `.venv` and `.env` plus the existing `agent-runtime` tunnel-client profile. When local Terminal capability is useful, keep `./start.sh` running in a foreground Terminal and create/refresh the existing ChatGPT plugin exposure against that live tunnel. Stopping that foreground process makes agent-runtime unavailable; ordinary ChatGPT/GitHub Executor capability remains the normal fallback and needs no runtime-specific ceremony.

## Tool surface

The MCP server exposes exactly one public tool:

- `terminal_exec(argv, cwd, timeout_seconds=300)` executes a non-empty argv list directly with `shell=False`, disconnected stdin, bounded output capture, and bounded timeout/process-group cleanup.

`AGENT_RUNTIME_WORKSPACE_ROOT` must name an absolute existing directory. `cwd` is realpath-checked to be at or below that root. This is only a working-directory/path-selection guard: executable arguments can still access other host paths using the operator account's normal permissions. It is **not** mechanical filesystem confinement.

Child commands receive only a small ordinary execution environment (`PATH`, `HOME`, `USER`, `TMPDIR`, `LANG`, and `LC_*` when present). Control-plane/runtime variables and token/key/credential-style ambient variables are not forwarded by default, and callers cannot supply an environment override.

There is no project registry, sync primitive, verifier state engine, command allowlist, approval broker, scheduler, autostart service, or orchestration layer.

## Verification

`./verify` runs deterministic tests and compile/shell-syntax checks. It does not require live control-plane credentials, a live tunnel, or a live ChatGPT plugin.
