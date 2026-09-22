# Protected Runtime Threat Model

## Boundary

Protected Runtime is a defense-in-depth **recognized intent** filter around Agent Runtime terminal entry points. It rejects recognized attempts to mutate the canonical Runtime lifecycle, tunnel, service, or protected listener.

It is **not a sandbox**, privilege boundary, syscall filter, filesystem confinement mechanism, or complete containment system. An allow result means only that this classifier did not recognize a protected intent.

## Protected assets

The filter reduces accidental or straightforward mutation of:

- the canonical Runtime process/process group;
- the canonical tunnel process;
- the ServiceManagement-owned lifecycle;
- the listener on `127.0.0.1:8080`;
- the intended protected singleton relationship among those components.

Configuration, installed package ownership, desired state, signing authority, and tunnel identity remain governed by their product lifecycle boundaries.

## Governance

Intentional lifecycle control belongs to the operator-facing lifecycle authority. **Executor governance** remains a separate defense-in-depth policy for repository work; it does not become lifecycle authority merely because Runtime transports a command.

## Recognized coverage

Current protection covers bounded command shapes implemented by the Runtime guard, including recognized direct signals, supported process-match forms, supported shell `-c`/`-lc` text, supported wrappers, canonical relaunch shapes, ServiceManagement/lifecycle actions, and recognized protected-port rebind/lifecycle attempts.

Recognized attempts fail as `PROTECTED_RUNTIME` with bounded audit metadata.

## Same UID limitation

Terminal tools execute with the operator account's normal authority. A program running as the operator's **same UID** can perform effects whose semantics are outside the classifier's recognized argv/text forms.

Therefore the filter is not complete same UID isolation. No allow result grants lifecycle authority.

## Explicit non-goals

The Runtime does not claim to prevent:

- arbitrary effects by every same UID process;
- effects through an unrecognized interpreter, script, library, debugger, or out-of-band tool;
- `root/sudo`;
- a **malicious local administrator**;
- kernel compromise or equivalent higher-privilege control;
- all host filesystem/process/network effects available to the operator account.

`AGENT_RUNTIME_WORKSPACE_ROOT` is an execution boundary enforced by Runtime semantics; it is not mechanical whole-host isolation.

## Permission boundary

There are no blanket TCC permissions for normal Runtime operation. Production `screen_capture` is intentionally governance-blocked as `VISUAL_PERCEPTION_BLOCKED` before native capture. That state is not evidence that Screen Recording permission is missing and must not trigger TCC changes.

Background Activity approval is separate ServiceManagement operator/platform state.

## Future isolation

Mechanically preventing arbitrary same UID programs from affecting protected Runtime resources would require a **separate future isolation architecture** with its own authority, compatibility analysis, implementation, and verification. This threat model does not claim that isolation today.
