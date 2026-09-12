#!/usr/bin/env bash
set -euo pipefail

fail() {
  echo "START ERROR: $*" >&2
  exit 2
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$ROOT"
ENV_FILE="$ROOT/.env"
LEGACY_CONFIG="$HOME/.config/tunnel-client/agent-runtime.yaml"
LABEL="com.picmao.agent-runtime-runtime"
DOMAIN="gui/$(id -u)"
SERVICE="$DOMAIN/$LABEL"
STATE_DIR="$HOME/Library/Application Support/Agent Runtime"
DESIRED_STATE="$STATE_DIR/protected-runtime-running"
LOCK_DIR="$STATE_DIR/lifecycle.lock"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
ACTION="${1:-start}"
MCP_COMMAND="command=$ROOT/.venv/bin/python -m agent_runtime.server,channel=main"
HEALTH_URL="http://127.0.0.1:8080"
RUNTIME_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

acquire_lock() {
  mkdir -p "$STATE_DIR"
  local i
  for i in {1..1000}; do
    if mkdir "$LOCK_DIR" 2>/dev/null; then
      trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT
      return 0
    fi
    sleep 0.01
  done
  fail "Timed out waiting for the lifecycle lock."
}

service_loaded() {
  launchctl print "$SERVICE" >/dev/null 2>&1
}

ensure_loaded() {
  [[ -f "$PLIST" && ! -L "$PLIST" ]] || fail "Missing canonical Runtime LaunchAgent at $PLIST; run ./install.sh first."
  if service_loaded; then
    return 0
  fi
  if launchctl bootstrap "$DOMAIN" "$PLIST" >/dev/null 2>&1; then
    return 0
  fi
  service_loaded || fail "Could not register canonical Runtime LaunchAgent."
}

set_running() {
  mkdir -p "$STATE_DIR"
  local tmp="$STATE_DIR/.protected-runtime-running.$$"
  : > "$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$DESIRED_STATE"
}
port_owner_pids() {
  command -v lsof >/dev/null 2>&1 || fail "lsof is required to protect port 8080."
  lsof -nP -iTCP:8080 -sTCP:LISTEN -t 2>/dev/null | sort -u || true
}

is_canonical_port_owner() {
  local pid="$1" command executable
  command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  executable="$(ps -p "$pid" -o comm= 2>/dev/null || true)"
  [[ "${executable##*/}" == "tunnel-client" ]] || return 1
  [[ "$command" == *"tunnel-client run"* ]] || return 1
  [[ "$command" == *"--control-plane.poll-channel main"* ]] || return 1
  [[ "$command" == *"--mcp.command $MCP_COMMAND"* ]] || return 1
  [[ "$command" == *"--health.listen-addr 127.0.0.1:8080"* ]] || return 1
  [[ "$command" != *"--profile"* ]]
}

preflight_protected_port() {
  local owners pid
  owners="$(port_owner_pids)"
  [[ -n "$owners" ]] || return 0
  service_loaded || fail "Protected port 8080 is occupied by an unsupervised or foreign process; refusing to kill or rebind it."
  while IFS= read -r pid; do
    [[ -n "$pid" ]] || continue
    is_canonical_port_owner "$pid" || fail "Protected port 8080 is occupied by a foreign or ambiguous process; refusing to kill or rebind it."
  done <<< "$owners"
}
READY_DIAGNOSTIC=""

runtime_ready_once() {
  local owners count pid
  owners="$(port_owner_pids)"
  count="$(printf '%s\n' "$owners" | awk 'NF { count++ } END { print count+0 }')"
  if [[ "$count" != "1" ]]; then
    READY_DIAGNOSTIC="expected exactly one listener on 127.0.0.1:8080; observed $count"
    return 1
  fi
  pid="$(printf '%s\n' "$owners" | awk 'NF { print; exit }')"
  if ! is_canonical_port_owner "$pid"; then
    READY_DIAGNOSTIC="port 8080 listener is not the canonical no-profile Runtime"
    return 1
  fi
  command -v curl >/dev/null 2>&1 || fail "curl is required for Runtime readiness checks."
  if ! curl -fsS --max-time 1 "$HEALTH_URL/healthz" >/dev/null 2>&1; then
    READY_DIAGNOSTIC="healthz is not green"
    return 1
  fi
  if ! curl -fsS --max-time 1 "$HEALTH_URL/readyz" >/dev/null 2>&1; then
    READY_DIAGNOSTIC="readyz is not green"
    return 1
  fi
  READY_DIAGNOSTIC="ready"
  return 0
}

wait_until_ready() {
  local i
  for i in {1..50}; do
    if runtime_ready_once; then
      return 0
    fi
    sleep 0.1
  done
  fail "Runtime did not become ready: $READY_DIAGNOSTIC"
}

wait_until_stopped() {
  local i owners pid
  for i in {1..30}; do
    owners="$(port_owner_pids)"
    if [[ -z "$owners" ]]; then
      return 0
    fi
    while IFS= read -r pid; do
      [[ -n "$pid" ]] || continue
      is_canonical_port_owner "$pid" \
        || fail "Port 8080 changed to a foreign or ambiguous owner while stopping; refusing to claim Runtime is stopped."
    done <<< "$owners"
    sleep 0.1
  done
  fail "Canonical Runtime listener did not stop within the bounded shutdown window."
}

serve() {
  local tunnel_client="${1:-}"
  [[ -f "$DESIRED_STATE" ]] || exit 0
  [[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || fail "Missing or invalid $ENV_FILE; run ./install.sh first."
  [[ ! -e "$LEGACY_CONFIG" && ! -L "$LEGACY_CONFIG" ]] \
    || fail "Legacy tunnel configuration must remain absent at $LEGACY_CONFIG."
  [[ "$tunnel_client" == /* && -x "$tunnel_client" && "${tunnel_client##*/}" == "tunnel-client" ]] \
    || fail "LaunchAgent must provide an absolute executable tunnel-client path."

  [[ -x "$ROOT/.venv/bin/python" ]] || fail "Missing Runtime Python at $ROOT/.venv/bin/python."

  exec /usr/bin/python3 - "$ENV_FILE" "$tunnel_client" "$ROOT/.venv/bin/python" "$RUNTIME_PATH" <<'PY'
import os
import re
import subprocess
import sys
from pathlib import Path

env_file = Path(sys.argv[1])
tunnel_client = sys.argv[2]
runtime_python = sys.argv[3]
runtime_path = sys.argv[4]

def fail(message):
    print("START ERROR: " + message, file=sys.stderr)
    raise SystemExit(2)

try:
    lines = env_file.read_text(encoding="utf-8").splitlines()
except OSError as exc:
    fail("Could not read checkout-local .env: " + str(exc))

required = {
    "CONTROL_PLANE_API_KEY",
    "CONTROL_PLANE_TUNNEL_ID",
    "AGENT_RUNTIME_WORKSPACE_ROOT",
}
values = {}
for number, line in enumerate(lines, start=1):
    if not line or line.lstrip().startswith("#"):
        continue
    match = re.fullmatch(r"([A-Z_][A-Z0-9_]*)=(.*)", line)
    if match is None:
        fail("Malformed .env entry at line " + str(number) + ".")
    key, value = match.groups()
    if key in required:
        if key in values:
            fail("Duplicate " + key + " entry in .env.")
        values[key] = value

missing = sorted(key for key in required if not values.get(key, ""))
if missing:
    fail("Missing non-empty .env value for " + ", ".join(missing) + ".")
workspace = Path(values["AGENT_RUNTIME_WORKSPACE_ROOT"])
if not workspace.is_absolute() or not workspace.is_dir():
    fail("AGENT_RUNTIME_WORKSPACE_ROOT must be an absolute existing directory.")

runtime_env = {
    "PATH": runtime_path,
    "HOME": os.environ.get("HOME", str(Path.home())),
    "CONTROL_PLANE_API_KEY": values["CONTROL_PLANE_API_KEY"],
    "CONTROL_PLANE_TUNNEL_ID": values["CONTROL_PLANE_TUNNEL_ID"],
    "AGENT_RUNTIME_WORKSPACE_ROOT": values["AGENT_RUNTIME_WORKSPACE_ROOT"],
    "OPEN_WEB_UI": "false",
}
for key in ("USER", "TMPDIR", "LANG"):
    value = os.environ.get(key)
    if value:
        runtime_env[key] = value
for key, value in os.environ.items():
    if key.startswith("LC_") and value:
        runtime_env[key] = value

common = [
    "--control-plane.poll-channel", "main",
    "--mcp.command", "command=" + runtime_python + " -m agent_runtime.server,channel=main",
    "--health.listen-addr", "127.0.0.1:8080",
]
doctor = subprocess.run(
    [tunnel_client, "doctor"] + common + ["--explain"],
    env=runtime_env,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    check=False,
)
if doctor.returncode != 0:
    fail("tunnel-client configuration check failed; Runtime was not started.")
os.execve(tunnel_client, [tunnel_client, "run"] + common, runtime_env)
PY
}

case "$ACTION" in
  --serve)
    serve "${2:-}"
    ;;
  start)
    acquire_lock
    preflight_protected_port
    ensure_loaded
    if runtime_ready_once; then
      echo "Agent Runtime desired state: RUNNING"
      exit 0
    fi
    set_running
    launchctl kickstart "$SERVICE" >/dev/null 2>&1 || fail "Could not start canonical Runtime service."
    wait_until_ready
    echo "Agent Runtime desired state: RUNNING"
    ;;
  stop)
    acquire_lock
    rm -f "$DESIRED_STATE"
    if service_loaded; then
      # A loaded PathState job may already have no live process to signal.
      # The bounded listener proof below is the authoritative STOPPED result.
      launchctl kill SIGTERM "$SERVICE" >/dev/null 2>&1 || true
    fi
    wait_until_stopped
    echo "Agent Runtime desired state: STOPPED"
    ;;
  restart)
    acquire_lock
    preflight_protected_port
    ensure_loaded
    # PathState KeepAlive would otherwise resurrect the old instance before
    # Restart can prove it stopped. This is an explicit operator transition.
    rm -f "$DESIRED_STATE"
    if service_loaded; then
      launchctl kill SIGTERM "$SERVICE" >/dev/null 2>&1 || true
      wait_until_stopped
    fi
    set_running
    launchctl kickstart "$SERVICE" >/dev/null 2>&1 || fail "Could not restart canonical Runtime service."
    wait_until_ready
    echo "Agent Runtime desired state: RUNNING"
    ;;
  status)
    if [[ -f "$DESIRED_STATE" ]]; then echo "RUNNING"; else echo "STOPPED"; fi
    ;;
  *)
    fail "Usage: ./start.sh [start|stop|restart|status|--serve <absolute-tunnel-client>]"
    ;;
esac
