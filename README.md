# agent-runtime

`agent-runtime` is a small, reusable, optional local execution plane for approved MCP clients. The normal ChatGPT-to-GitHub workflow does not depend on it. Use it only when a local disposable checkout must be synchronized or verified under a narrow trusted policy.

It is not an orchestrator, remote shell, GitHub writer, deployment system, scheduler, agent framework, Docker abstraction, or product-specific application runtime. Codex/Luna adapters, dynamic repository discovery, dashboards, queues, databases, and GitHub Actions are deliberately outside v1.

## Architecture

```text
ChatGPT / approved MCP client
        |
        v
agent-runtime MCP gateway
        |
        v
trusted local project profile
        |
        v
disposable local checkout
        |
        v
project-native verifier
```

The model chooses only a bounded `project` ID. The trusted local TOML profile chooses the checkout path, Git remote and exact expected remote URL, branch, verifier argv, timeout, state directory, and whether destructive sync is permitted. Profiles are never writable through MCP.

## Public MCP surface

Exactly four public tools exist:

- `get_head(project)` reads bounded local Git metadata and current lock state without fetching the network.
- `sync(project)` destructively mirrors an explicitly disposable checkout to its configured remote branch.
- `run_verify(project)` validates the checkout, captures its exact HEAD, writes local RUNNING state, and launches a detached verification worker.
- `get_last_log(project)` returns latest verification state and at most a 64 KiB diagnostic log tail.

There is no shell/exec tool, arbitrary path/branch/verifier argument, Git push/commit/branch tool, filesystem mutation endpoint, environment mutation endpoint, or profile mutation endpoint.

## Install on macOS

For the ChatGPT-to-local tunnel use case, the supported bootstrap flow is:

```bash
git clone https://github.com/phatnguyen03022001/agent-runtime.git
cd agent-runtime
./install.sh
./start.sh
```

`install.sh` is fail-closed and does not start the long-lived tunnel daemon. It requires macOS, Git, Python 3.11+, and Homebrew only when `tunnel-client` is missing. A missing tunnel client is installed only from the official OpenAI Homebrew formula `openai/tools/tunnel-client`.

The installer creates or validates the local `.venv`, runs `./verify`, prepares private machine-local runtime state/configuration, creates a separate disposable self-verification checkout, updates only installer-owned non-secret `.env` values, creates the `agent-runtime` tunnel profile only when absent, and requires `tunnel-client doctor` to pass. Unknown or incompatible pre-existing local state is preserved and blocks rather than being reset or deleted.

`CONTROL_PLANE_API_KEY` and `CONTROL_PLANE_TUNNEL_ID` remain operator-supplied secrets. Put them in the ignored `.env` file or export them before rerunning `./install.sh`. The installer never prints their literal values. `./start.sh` remains the explicit foreground daemon start path.

## Trusted project profiles

Copy `config/profiles.example.toml` to a machine-local path such as `runtime.local.toml` and keep it out of Git. Configuration uses Python's stdlib `tomllib` and fails closed on unsupported versions, unknown fields, invalid project IDs, relative state/checkout paths, invalid verifier argv, non-boolean `disposable`, or timeout outside `1..3600` seconds.

A profile resembles:

```toml
version = 1
state_dir = "/absolute/local/state/path"

[projects.example-main]
repository = "owner/repo"
checkout = "/absolute/path/to/disposable-checkout"
remote = "origin"
expected_remote_url = "git@github.com:owner/repo.git"
branch = "main"
verify_argv = ["./verify"]
timeout_seconds = 3600
disposable = true
```

The configured Git remote URL is checked exactly before destructive sync and verification. Finding a Git repository at a path is not sufficient proof of identity. The configured checkout must also resolve to exactly the Git worktree top level reported by `git rev-parse --show-toplevel`; a subdirectory of a larger repository fails closed before destructive sync or trusted verification use. An exact linked-worktree top level remains supported.

## Destructive sync warning

`sync(project)` is intentionally destructive and refuses profiles unless `disposable = true`. It performs the bounded equivalent of:

```text
git fetch <remote> <branch> --prune
git switch <branch>
git reset --hard <remote>/<branch>
git clean -fd
```

Tracked changes and non-ignored untracked files are discarded. Ignored files survive because `git clean -fdx` is never used. Runtime code never commits, pushes, or creates target-repository branches.

## Verification semantics

`run_verify(project)` requires the expected remote identity, exact configured worktree root, configured branch, clean worktree, and exact equality between local HEAD and the cached configured remote branch. It also requires the on-disk trusted config bytes and worker-relevant runtime source to match the immutable generation captured when `AgentRuntime` loaded; generation drift is refused until the runtime is restarted/reloaded. It captures the verification HEAD and launches a detached worker while transferring the same `fcntl.flock` lock descriptor, so there is no unlocked handoff window.

The detached worker receives the same expected config/runtime generation identity, revalidates it before resolving the verifier profile, and checks it again immediately before verifier execution. It therefore cannot silently adopt a newer `verify_argv`, profile, or worker-relevant runtime source after the launcher accepted an older generation.

The verifier argv comes only from trusted configuration and is executed as a subprocess argv array, never through `shell=True`. The verifier runs in a POSIX process group with a bounded timeout. On timeout, the whole verifier process group is terminated before terminal state is finalized.

The verifier is trusted target executable code; `agent-runtime` is not an OS-effect sandbox, and hostile `setsid()`/daemon escape is outside the current threat model. The verifier receives only the runtime-selected `PATH` environment (`{"PATH": os.environ.get("PATH", os.defpath)}`); unrelated parent runtime variables, tunnel credentials, cloud tokens, and shell secrets are not inherited, and MCP/model input cannot select verifier environment variables. The verifier can still read target files and emit data it legitimately obtains itself.

Verifier exit code `0` is necessary but insufficient for PASS. PASS additionally requires the final HEAD to equal the captured HEAD, the configured branch still active, a clean worktree, exact synchronization with the cached configured remote branch, matching remote identity, and no timeout/interruption. Any failed postcondition produces FAIL rather than manufactured success.

## Local state and logs

State lives only under the configured external `state_dir`:

```text
<state_dir>/<project>/
  runner.lock
  verify-state.json
  verify-state.json.tmp
  last-verify.log
  last-verify.log.inprogress
```

State writes are serialized JSON capped at 256 KiB and committed with temp-file + `os.replace`. Only the latest current/completed log paths are kept. The entire persisted in-progress/final diagnostic log for one verification run is capped at exactly 1 MiB (1,048,576 bytes), including runtime markers; overflow terminates verification and yields `failure_kind = "verify_log_limit_exceeded"`. `get_last_log` still returns at most a 64 KiB diagnostic log tail. Active state whose lock is no longer held is reread before interruption is synthesized, so a concurrently committed terminal state and its finalized log win; a still-active free-lock state remains `INTERRUPTED`, never PASS. Guarded stale/corrupt-state recovery occurs only while the mutation lock is held. Do not blindly delete lock files.

## Python setup

Python 3.11+ is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

The only direct runtime dependency is pinned `mcp==2.0.0`; runtime logic otherwise uses the Python standard library.

## Start locally

Set an absolute trusted config path, then run the MCP server directly:

```bash
export AGENT_RUNTIME_CONFIG=/absolute/path/to/runtime.local.toml
python -m agent_runtime.server
```

## Start through tunnel-client

The recommended macOS path is `./install.sh` once, then `./start.sh` whenever the tunnel should run. For manual setup, copy `.env.example` to `.env`, set `AGENT_RUNTIME_CONFIG`, and configure the external tunnel-client profile. `AGENT_RUNTIME_TUNNEL_PROFILE` defaults to `agent-runtime`.

```bash
./start.sh
```

`start.sh` derives its own repository directory, loads `.env`, runs `tunnel-client doctor --profile <profile> --explain`, and only then executes `tunnel-client run --profile <profile>`. The tunnel client and networking/control plane remain external to this repository.

## Reload after runtime or profile changes

Changes to runtime code, `.env` values loaded by `start.sh`, `AGENT_RUNTIME_CONFIG`, or trusted project profile files are not adopted by an already-running runtime/tunnel process. Restart or reload that process before relying on the new configuration. For `run_verify`, the runtime enforces this boundary by comparing the current config bytes and worker-relevant runtime source generation with the identity captured at runtime load, and the detached worker revalidates the same identity before executing the verifier. A shell or terminal reset is not required merely because configuration changed unless the active setup depends on shell-exported environment outside `.env`.

After restarting, use a fresh-capability context when runtime/config/profile changed and current capability exposure is absent, plausibly stale, or ambiguous, or when the current conversation has not yet proven the restarted runtime callable. Use `get_head(project)` as the first runtime smoke/preflight action. A successful `get_head(project)` in the current conversation after the latest relevant change is sufficient callability evidence when no later runtime-changing event occurred; do not open a fresh chat merely as ceremony. Use `sync(project)` only when the disposable local checkout must be synchronized, and use `run_verify(project)` with `get_last_log(project)` only when local verification evidence materially matters.

## Verify agent-runtime itself

```bash
./verify
```

The root verifier runs stdlib `unittest`, compiles all runtime modules, and performs only cheap deterministic guards for dangerous regressions.

## Security model

The public capability boundary is the four project-ID-only MCP methods. Callers cannot provide commands, executables, verifier argv, paths, checkouts, remotes, branches, Docker arguments, environment variables, timeouts, cleanup commands, or profile updates. Local trusted profiles hold dangerous choices; runtime repository operations are bounded to inspect, fetch, switch to the configured existing branch, hard reset, and `clean -fd`.

Secrets, `.env`, local profiles, virtual environments, runtime state, logs, and lock files are ignored. Example files contain placeholders only.

## Migrating from ielts-tunnel

v1 is derived from the proven `phatnguyen03022001/ielts-tunnel` runtime mechanics at revision `303979c815d6880e1681faddcb3fc0c1d842e5b0`: its narrow four-tool boundary, `fcntl.flock` serialization, zero-gap inherited lock handoff, detached verifier, process-group timeout cleanup, atomic state, bounded log reads, fail-closed stale recovery, destructive `clean -fd` sync, and verification postconditions were preserved and generalized into trusted profiles.

No live IELTS tunnel migration occurs here. A future local profile could conceptually be:

```toml
[projects.ielts-main]
repository = "phatnguyen03022001/ilets"
checkout = "/trusted/local/disposable/ielts-runner"
remote = "origin"
expected_remote_url = "<trusted exact IELTS origin URL>"
branch = "main"
verify_argv = ["./tools/verify-local"]
timeout_seconds = 3600
disposable = true
```

Then the old conceptual surface maps as `get_head` -> `get_head("ielts-main")`, `sync_main` -> `sync("ielts-main")`, `run_verify` -> `run_verify("ielts-main")`, and `get_last_log` -> `get_last_log("ielts-main")`. No IELTS-specific path, verifier, branch, or behavior is hard-coded in generic runtime modules.
