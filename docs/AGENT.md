# Agent Procedure

This is the deterministic procedure for an AI agent operating Agent Runtime without historical task/chat knowledge.

## Inputs

Allowed product inputs:

- `README.md`
- `docs/*`
- `.env.example`
- `./install.sh --help`
- `./start.sh --help`
- `./install.sh --check --json`
- `./start.sh doctor --json`
- `runtime_capabilities` when MCP is already reachable

Do not read governance-history files to operate the product.

## Procedure

1. Run:
   ```bash
   ./install.sh --check --json
   ```
2. If status is `blocked`, inspect only the failing check's `reason_code` and `action_class`.
3. Route exactly:
   - `SAFE_AUTOMATED`: perform only the bounded action documented for that reason.
   - `HUMAN_ACTION_REQUIRED`: stop and ask the operator for the required credential, signing authority, platform approval, or cutover judgment.
   - `STOP_AND_ESCALATE`: stop. Preserve non-secret evidence. Do not repair by guessing.
4. When preflight is `ready`, use the canonical installer if installation/update is authorized:
   ```bash
   ./install.sh
   ```
5. Start only through:
   ```bash
   ./start.sh start
   ```
6. Diagnose only through:
   ```bash
   ./start.sh doctor --json
   ```
7. For non-OK doctor output, route the `reason_code` using [RECOVERY.md](RECOVERY.md), perform at most the documented action, then rerun doctor.
8. A pending cutover requires human acceptance judgment before:
   ```bash
   ./install.sh --commit-cutover
   ```
   or:
   ```bash
   ./install.sh --rollback-cutover
   ```

## Hard stops

Never:

- guess hidden configuration keys;
- print or persist `CONTROL_PLANE_API_KEY`, the complete tunnel ID, Runtime Git name/email, or signing credentials;
- inspect Keychain identities to choose a signer;
- open System Settings automatically;
- modify TCC;
- blindly kill or rebind a process on port 8080;
- blindly delete/recreate an installed app, service registration, or cutover transaction;
- treat `VISUAL_PERCEPTION_BLOCKED` as a Screen Recording permission problem;
- use an internal service entrypoint as a normal operator command.

If JSON cannot be parsed, authority is ambiguous, or a reason code is absent from the recovery matrix, STOP_AND_ESCALATE.
