# Operations

This document owns normal installed-product operation.

## Lifecycle

```bash
./start.sh start
./start.sh stop
./start.sh restart
./start.sh status
./start.sh session-limit
```

After installation, source-checkout lifecycle commands delegate to the package-owned helper. The internal service entrypoint is not a normal operator command.

Desired state is explicit and persistent:

- `start` records RUNNING only through the governed lifecycle path and waits for readiness.
- `stop` records STOPPED and suppresses automatic recovery until the next explicit start.
- `restart` performs one bounded explicit restart.
- `status` reports desired state and effective persistent-session capacity.

Foreign or ambiguous ownership of `127.0.0.1:8080` fails closed. Do not kill/rebind unknown owners.

## Doctor

```bash
./start.sh doctor
./start.sh doctor --json
```

This is the operator diagnostic authority. It uses installed package bytes plus canonical `runtime.env`; it does not repair state.

Direct `python -m agent_runtime.doctor` is a source/development diagnostic and is not the installed-product operator entrypoint.

## OpenAI tunnel health

The product transport is OpenAI Secure MCP Tunnel. `tunnel-client` connects outbound to OpenAI and keeps the Runtime private. Ordinary readiness requires exactly one canonical loopback listener and green:

```text
http://127.0.0.1:8080/healthz
http://127.0.0.1:8080/readyz
```

Do not expose the local health/admin surface publicly.

## Installed paths

```text
~/Applications/Agent Runtime.app
~/Applications/Agent Runtime.app/Contents/Resources/runtime/
~/Library/Application Support/Agent Runtime/runtime.env
~/Library/Application Support/Agent Runtime/protected-runtime-running
~/Library/Application Support/Agent Runtime/cutover-transaction/
```

Canonical `runtime.env` is operator-owned configuration and must stay mode `0600`.

## Update

Run preflight before a new package transaction:

```bash
./install.sh --check
./install.sh
```

Do not begin a second update while a cutover transaction is present. After acceptance, choose exactly one:

```bash
./install.sh --commit-cutover
./install.sh --rollback-cutover
```

## Uninstall

```bash
./install.sh --uninstall
```

Uninstall removes only attributable product-owned state and retains canonical `runtime.env` by default. It does not reset TCC, purge unrelated Login Items, delete credentials, or clean unrelated processes/files.
