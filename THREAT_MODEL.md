# Protected Runtime Threat Model

## Purpose and boundary

Protected Runtime is a defense-in-depth recognized intent filter around Agent Runtime terminal entry points. It classifies the argv or supported shell text presented at that boundary and rejects recognized attempts to mutate the canonical Runtime lifecycle, tunnel, service, or protected listener.

It is not a sandbox, privilege boundary, syscall filter, filesystem confinement mechanism, or complete containment system. A command that is not classified as protected is not thereby safe or authorized.

This contract documents the current protection boundary. It does not expand enforcement.

## Protected assets

The filter is intended to reduce accidental or straightforward mutation of:

- the canonical Agent Runtime process and process group;
- the canonical tunnel process;
- the Runtime launchd service and explicit lifecycle entry points;
- the listener bound to `127.0.0.1:8080`;
- the intended singleton relationship among those Runtime components.

Runtime configuration, desired state, installed package ownership, and tunnel identity remain governed by their existing lifecycle and repository contracts.

## Authority and governance

Intentional Runtime lifecycle control belongs to the operator-facing lifecycle authority and to separately authorized infrastructure-maintenance work. Ordinary repository task execution does not inherit that authority merely because Agent Runtime transports the work.

Executor governance is a separate control plane. It continues to forbid lifecycle mutation of shared Agent Runtime infrastructure unless exact current authority permits it. The classifier is therefore defense-in-depth beneath governance, not a replacement for governance.

## Recognized intents covered

Current protection recognizes bounded command shapes already implemented by `ProtectedRuntimeGuard`, including:

- direct signals to identified canonical Runtime/tunnel processes or process groups;
- recognized process-match operations targeting canonical processes;
- supported `sh`, `bash`, or `zsh` `-c` / `-lc` shell text containing a recognized protected action;
- supported command wrappers that unwrap to an already recognized protected action;
- canonical Runtime relaunch command shapes;
- launchd and Runtime CLI lifecycle operations for the canonical service;
- recognized protected-port lifecycle and rebind attempts involving port `8080`.

A recognized protected intent is rejected as `PROTECTED_RUNTIME` and produces only the existing bounded category/tool/timestamp audit record.

## Same-UID arbitrary-program limitation

Terminal commands run with the operator account's normal host authority. The classifier observes command arguments and the recognized shell/wrapper forms it explicitly understands; it does not interpret the semantics of every executable that the same UID may run.

Consequently, an otherwise allowed same UID program can perform host effects that are not visible as a recognized protected intent at the classifier boundary. Representative synthetic classifier-only forms include:

- Python with inline code;
- Perl with inline code;
- a shell interpreter given a script path rather than recognized `-c` / `-lc` text.

Those forms are regression fixtures for classification semantics only. The limitation payloads must never be executed by the tests.

No allow result grants lifecycle authority. It means only that this argv/text classifier did not recognize a protected intent in the supplied form.

## Explicit non-goals

Protected Runtime does not claim to prevent:

- arbitrary effects by every process running as the operator's same UID;
- effects performed through an unrecognized interpreter, script, library, debugger, or out-of-band tool;
- `root` / `sudo`, a malicious local administrator, kernel compromise, or equivalent higher-privilege control;
- general filesystem access available to executed programs under the operator account;
- all possible process, network, launchd, or syscall mechanisms that could affect Runtime.

It also does not turn `AGENT_RUNTIME_WORKSPACE_ROOT` into mechanical host isolation. Existing `fs_read_batch` descriptor-relative no-symlink rules are a separate bounded read contract and do not change terminal-process authority.

## Future isolation boundary

Mechanically preventing arbitrary same UID programs from affecting protected Runtime resources would require a separate future isolation architecture with its own authority, threat model, compatibility analysis, implementation, and verification.

That architecture is outside this contract and outside TASK-0043. Protected Runtime remains a small recognized-intent defense layer rather than growing interpreter-specific rules that would imply coverage it cannot provide.
