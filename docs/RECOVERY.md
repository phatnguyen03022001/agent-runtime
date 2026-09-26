# Recovery

Recovery is reason-code driven. Do not replace a specific reason code with a blind reinstall, kill, or permission change.

## Action classes

- **SAFE_AUTOMATED** — bounded documented action may be executed without new operator judgment.
- **HUMAN_ACTION_REQUIRED** — credentials, signing authority, platform approval, or commit-vs-rollback judgment is required.
- **STOP_AND_ESCALATE** — ownership/authority is ambiguous or corrupted; do not perform blind repair.

## Doctor reason-code routing

Each material non-OK doctor reason code has exactly one action class.

| Reason code | Action class | Safe next action |
| --- | --- | --- |
| `WORKSPACE_UNAVAILABLE` | HUMAN_ACTION_REQUIRED | Correct canonical `AGENT_RUNTIME_WORKSPACE_ROOT`, then rerun doctor. |
| `WORKSPACE_INVALID` | HUMAN_ACTION_REQUIRED | Choose an existing absolute workspace and correct canonical configuration. |
| `RUNTIME_LIMIT_CONFIG_INVALID` | HUMAN_ACTION_REQUIRED | Correct session/parallelism bounds in canonical configuration. |
| `GIT_UNAVAILABLE` | HUMAN_ACTION_REQUIRED | Install/select Apple developer tools so fixed Git is available. |
| `GIT_IDENTITY_UNAVAILABLE` | HUMAN_ACTION_REQUIRED | Provide a valid Runtime Git name/email pair without printing it. |
| `APP_NOT_INSTALLED` | SAFE_AUTOMATED | Run `./install.sh --check`; if READY, run the canonical installer. |
| `INSTALLED_PACKAGE_INVALID` | STOP_AND_ESCALATE | Preserve evidence; do not overwrite or delete the ambiguous package. |
| `SERVICE_APPROVAL_REQUIRED` | HUMAN_ACTION_REQUIRED | Approve Background Activity in macOS, then rerun doctor. |
| `SERVICE_NOT_REGISTERED` | STOP_AND_ESCALATE | Inspect recognized cutover state before any registration/reinstall action. |
| `SERVICE_STATUS_INVALID` | STOP_AND_ESCALATE | Preserve ServiceManagement evidence; do not guess registration state. |
| `CUTOVER_TRANSACTION_PRESENT` | HUMAN_ACTION_REQUIRED | Inspect recognized transaction status and choose resume/commit/rollback as appropriate. |
| `CUTOVER_STATE_INVALID` | STOP_AND_ESCALATE | Preserve the transaction directory; do not delete or reconstruct metadata blindly. |
| `RUNTIME_IDENTITY_MISMATCH` | STOP_AND_ESCALATE | Stop product changes and review installed/source identity provenance. |
| `CAPABILITY_REGISTRY_MISMATCH` | STOP_AND_ESCALATE | Stop; public capability authority is inconsistent. |
| `SCHEMA_EXPORT_INVALID` | STOP_AND_ESCALATE | Stop; schema authority could not be validated. |
| `TOOL_CONTRACT_SCHEMA_MISMATCH` | STOP_AND_ESCALATE | Stop; ToolContract/schema projection is inconsistent. |
| `GOVERNANCE_PROTECTION_INVALID` | STOP_AND_ESCALATE | Stop; protection/governance identity is inconsistent. |
| `INTERNAL_SERIALIZATION_FAILURE` | STOP_AND_ESCALATE | Preserve stderr/exit status without secrets and escalate. |

## Cutover states

A recognized cutover may be `AWAITING_APPROVAL`, `PENDING`, or `PARTIAL`.

- **AWAITING_APPROVAL**: HUMAN_ACTION_REQUIRED. Resolve the macOS approval boundary, then use the documented resume path.
- **PENDING**: HUMAN_ACTION_REQUIRED. Downstream acceptance must decide commit or rollback.
- **PARTIAL**: run the bounded partial recovery command only when the transaction metadata is recognized:

  ```bash
  ./install.sh --recover-partial-cutover
  ```

Use `./install.sh --resume-cutover` only for a recognized resumable transaction. Never delete the transaction directory manually to “unstick” installation.

## Protected port ownership

If port 8080 is owned by a foreign or ambiguous process, stop and escalate. Do not kill, signal, rebind, or claim ownership based only on the port number.

## Package/config/service failures

- Missing app: preflight first, then canonical install when READY.
- Invalid installed package: STOP_AND_ESCALATE.
- Missing/invalid canonical `runtime.env`: HUMAN_ACTION_REQUIRED unless ownership is ambiguous, in which case stop.
- Service approval: HUMAN_ACTION_REQUIRED.
- Unrecognized ServiceManagement state: STOP_AND_ESCALATE.
- Production `screen_capture=VISUAL_PERCEPTION_BLOCKED`: this is governance, not a permission failure. Do not request Screen Recording or alter TCC.

## Uninstall recovery

Normal removal:

```bash
./install.sh --uninstall
```

If uninstall refuses because ownership or transaction state is ambiguous, preserve the state and stop. Do not manually purge app/service/config paths. Canonical `runtime.env` is retained by default.

## Source execution recovery

The checkout source uses one process-local lifecycle for PTY and pipe sessions. A keyed start or `terminal_exec` call is reserved before process dispatch; while its result is retained, the same key and exact execution specification identify the original operation. A conflicting specification fails before another process can start.

The shared session-result projection applies one 16 KiB hard raw-byte limit and caller budget to merged PTY text and ordered stdout/stderr pipe chunks. Source `terminal_poll` uses request/result schemas v4/v4, defaults to incremental output, and accepts a smaller `max_output_bytes` budget. `output: none` changes only text projection: it reports status, lifecycle, exit information, and cursor-expiry accounting while returning empty output and consuming no retained bytes. For a valid cursor, its `next_cursor` is the requested cursor or retained base, whichever is greater; a cursor ahead of available output is rejected. A later incremental poll can therefore read the same output. `wait_for` continues to decide whether polling waits for output/state or terminal/deadline. Valid UTF-8 code points are kept whole across poll-budget and pipe-read boundaries. If the budget cannot fit the next complete code point, no cursor progress is made past it; request a larger budget. Invalid byte sequences keep replacement decoding.

Keyed results are retained for up to 3600 seconds and at most 16 completed sessions. After eviction or a Runtime restart, an unknown key cannot prove that a prior process or mutation never ran. Reconcile the external consequence before repeating a mutation; do not treat a lost response or unknown key as retry permission. These source contracts are active in the installed Runtime 0.5.0.
