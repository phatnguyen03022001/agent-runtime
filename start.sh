#!/usr/bin/env bash
set -euo pipefail

fail() {
  echo "START ERROR: $*" >&2
  exit 2
}

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
INSTALLED_RUNTIME_ROOT="${HOME}/Applications/Agent Runtime.app/Contents/Resources/runtime"

# Source checkout commands converge on installed lifecycle bytes when present.
# --serve is private to the installed supervisor and doctor always checks the
# installed authority explicitly.
if [[ "${1:-start}" != "--serve" \
      && "${1:-start}" != "doctor" \
      && "${1:-start}" != "--help" \
      && "${1:-start}" != "-h" \
      && "$SOURCE_ROOT" != "$INSTALLED_RUNTIME_ROOT" \
      && "${AGENT_RUNTIME_USE_SOURCE_RUNTIME:-0}" != "1" \
      && -x "$INSTALLED_RUNTIME_ROOT/start.sh" ]]; then
  exec "$INSTALLED_RUNTIME_ROOT/start.sh" "$@"
fi

ROOT="$SOURCE_ROOT"
cd "$ROOT"
STATE_DIR="$HOME/Library/Application Support/Agent Runtime"
CANONICAL_ENV_FILE="$STATE_DIR/runtime.env"
PAYLOADS_ROOT="$STATE_DIR/payloads"
PAYLOAD_POINTER="$STATE_DIR/current-payload"
if [[ "$SOURCE_ROOT" == "$INSTALLED_RUNTIME_ROOT" ]]; then
  ENV_FILE="$CANONICAL_ENV_FILE"
else
  ENV_FILE="${RUNTIME_ENV_FILE:-$ROOT/.env}"
fi
LEGACY_CONFIG="$HOME/.config/tunnel-client/agent-runtime.yaml"
CURRENT_RUNTIME_LAUNCHD_LABEL="com.picmao.agent-runtime-runtime-service"
DOMAIN="gui/$(id -u)"
SERVICE="$DOMAIN/$CURRENT_RUNTIME_LAUNCHD_LABEL"
LOCK_DIR="$STATE_DIR/lifecycle.lock"
INSTALLED_APP="$HOME/Applications/Agent Runtime.app"
RUNTIME_SERVICE_PLIST="$HOME/Library/LaunchAgents/$CURRENT_RUNTIME_LAUNCHD_LABEL.plist"
RUNTIME_SERVICE_HELPER="$INSTALLED_APP/Contents/MacOS/AgentRuntimeRuntimeService"
ACTION="${1:-start}"
RUNTIME_PYTHON="$ROOT/.venv/bin/python"
MCP_COMMAND="command=${RUNTIME_PYTHON// /\\ } -m agent_runtime.server,channel=main"
MCP_COMMAND_NORMALIZED="command=$RUNTIME_PYTHON -m agent_runtime.server,channel=main"
HEALTH_URL="http://127.0.0.1:8080"
RUNTIME_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
DEFAULT_SESSION_LIMIT=6

doctor_error() {
  local reason="$1"
  local message="$2"
  local json_mode="${3:-0}"
  if [[ "$json_mode" == "1" ]]; then
    printf '{"error":{"message":"%s","reason_code":"%s"}}\n' "$message" "$reason"
  else
    printf 'START ERROR: %s (%s)\n' "$message" "$reason" >&2
  fi
  exit 2
}

validate_installed_selection() {
  local runtime_root="$1"
  local runtime_python="$2"
  [[ -x "$runtime_python" && ! -L "$runtime_python" ]] \
    || fail "Bundled Runtime Python is missing or unsafe."
  "$runtime_python" - "$runtime_root" "$PAYLOADS_ROOT" "$PAYLOAD_POINTER" <<'PY'
import hashlib
import os
import re
import stat
import sys
from pathlib import Path

runtime_root = Path(sys.argv[1])
payloads_root = Path(sys.argv[2])
pointer = Path(sys.argv[3])
sys.dont_write_bytecode = True
sys.path.insert(0, str(runtime_root / "macos"))

def fail(message):
    print("START ERROR: " + message, file=sys.stderr)
    raise SystemExit(2)

try:
    import package_provenance as provenance
except ImportError:
    fail("Installed Runtime package provenance is unavailable.")

manifest_path = runtime_root.parent / "runtime-manifest.json"
try:
    manifest = provenance._load_substrate_manifest(manifest_path)
    provenance.validate_substrate_manifest(
        runtime_root,
        manifest_path,
        manifest.get("runtime_revision"),
        manifest.get("git_tree"),
        manifest.get("requirements_lock_sha256"),
    )
except (OSError, provenance.PackageProvenanceError):
    fail("Installed Runtime substrate provenance is invalid.")

try:
    info = pointer.lstat()
except OSError:
    fail("Current payload pointer is missing.")
if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
    fail("Current payload pointer is unsafe.")
if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
    fail("Current payload pointer ownership or mode is unsafe.")
try:
    raw_pointer = pointer.read_text(encoding="ascii")
except (OSError, UnicodeError):
    fail("Current payload pointer is unreadable.")
match = re.fullmatch(r"([0-9a-f]{64})\n", raw_pointer)
if match is None:
    fail("Current payload pointer is malformed.")
closure = match.group(1)
release = payloads_root / closure
if release.is_symlink() or not release.is_dir():
    fail("Selected external Runtime payload release is missing or unsafe.")
try:
    payload = provenance.validate_payload_release(
        release,
        expected_closure=closure,
        expected_requirements_lock_sha256=manifest["requirements_lock_sha256"],
        expected_python_major_minor=manifest["required_python_major_minor"],
        expected_public_tool_count=manifest["expected_public_tool_count"],
        expected_public_surface_sha256=manifest["expected_public_surface_sha256"],
    )
except (OSError, provenance.PackageProvenanceError):
    fail("Selected external Runtime payload provenance is invalid.")

# The selected release is the only first-party import root. Validate the actual
# advertised surface before the tunnel is allowed to execute the module.
sys.path.insert(0, str(release))
try:
    from agent_runtime.capability_registry import ADVERTISED_TOOL_NAMES
except Exception:
    fail("Selected external Runtime payload cannot expose the public tool registry.")
blob = ("\n".join(ADVERTISED_TOOL_NAMES) + "\n").encode("utf-8")
actual_count = len(ADVERTISED_TOOL_NAMES)
actual_surface = hashlib.sha256(blob).hexdigest()
if actual_count != manifest["expected_public_tool_count"] or actual_count != 20:
    fail("Selected Runtime payload public tool count is incompatible.")
if actual_surface != manifest["expected_public_surface_sha256"]:
    fail("Selected Runtime payload public tool surface is incompatible.")
if payload["expected_public_tool_count"] != actual_count or payload["expected_public_surface_sha256"] != actual_surface:
    fail("Selected Runtime payload manifest public tool contract is inconsistent.")
print(f"{release}\t{payload['source_revision']}")
PY
}

require_current_launchagent() {
  [[ -x "$RUNTIME_SERVICE_HELPER" && -f "$RUNTIME_SERVICE_HELPER" && ! -L "$RUNTIME_SERVICE_HELPER" ]] \
    || fail "Current Runtime supervisor executable is missing or unsafe."
  [[ -f "$RUNTIME_SERVICE_PLIST" && ! -L "$RUNTIME_SERVICE_PLIST" ]] \
    || fail "Current Runtime LaunchAgent plist is missing or unsafe."
  /usr/bin/python3 - "$RUNTIME_SERVICE_PLIST" "$RUNTIME_SERVICE_HELPER" "$CURRENT_RUNTIME_LAUNCHD_LABEL" "$(id -u)" <<'PY'
import os
import plistlib
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
helper = sys.argv[2]
label = sys.argv[3]
uid = int(sys.argv[4])

def fail(message):
    print("START ERROR: " + message, file=sys.stderr)
    raise SystemExit(2)

try:
    info = path.lstat()
except OSError:
    fail("Current Runtime LaunchAgent plist is unavailable.")
if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
    fail("Current Runtime LaunchAgent plist is unsafe.")
if info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o600:
    fail("Current Runtime LaunchAgent ownership or mode is unsafe.")
try:
    value = plistlib.loads(path.read_bytes())
except (OSError, plistlib.InvalidFileException):
    fail("Current Runtime LaunchAgent plist is malformed.")
expected = {
    "Label": label,
    "ProgramArguments": [helper],
    "RunAtLoad": False,
    "KeepAlive": False,
    "ProcessType": "Interactive",
    "ThrottleInterval": 2,
}
if value != expected:
    fail("Current Runtime LaunchAgent identity or contract is foreign.")
PY
  service_loaded || fail "Current Runtime LaunchAgent is not loaded."
}

installed_doctor() {
  local json_mode=0
  local doctor_arg=""
  if [[ "$#" == "1" && "${1:-}" == "--json" ]]; then
    json_mode=1
    doctor_arg="--json"
  elif [[ "$#" != "0" ]]; then
    fail "Usage: ./start.sh doctor [--json]"
  fi

  local doctor_root="$INSTALLED_RUNTIME_ROOT"
  local doctor_python="$doctor_root/.venv/bin/python"
  local doctor_env="$CANONICAL_ENV_FILE"
  local installed_app="$HOME/Applications/Agent Runtime.app"
  if [[ ! -e "$installed_app" && ! -L "$installed_app" ]]; then
    doctor_error "APP_NOT_INSTALLED" "Canonical Agent Runtime app is not installed." "$json_mode"
  fi
  [[ -d "$installed_app" && ! -L "$installed_app" && -x "$doctor_python" ]] \
    || doctor_error "INSTALLED_PACKAGE_INVALID" "Installed Runtime substrate is incomplete or unsafe." "$json_mode"
  [[ -f "$doctor_env" && ! -L "$doctor_env" ]] \
    || doctor_error "CANONICAL_CONFIG_MISSING" "Canonical runtime.env is missing or unsafe." "$json_mode"

  local selection selected_payload runtime_revision selection_reason selection_message
  if ! selection="$(validate_installed_selection "$doctor_root" "$doctor_python" 2>&1)"; then
    selection_reason="PAYLOAD_RELEASE_INVALID"
    selection_message="Selected external Runtime payload is invalid."
    case "$selection" in
      *"Installed Runtime substrate provenance is invalid."*)
        selection_reason="INSTALLED_SUBSTRATE_INVALID"
        selection_message="Installed immutable Runtime substrate is invalid."
        ;;
      *"Current payload pointer is missing."*)
        selection_reason="PAYLOAD_POINTER_MISSING"
        selection_message="Canonical current-payload pointer is missing."
        ;;
      *"Current payload pointer"*"unsafe."*|*"Current payload pointer"*"malformed."*|*"Current payload pointer"*"unreadable."*|*"Current payload pointer ownership or mode is unsafe."*)
        selection_reason="PAYLOAD_POINTER_INVALID"
        selection_message="Canonical current-payload pointer is invalid."
        ;;
      *"Selected external Runtime payload release is missing or unsafe."*)
        selection_reason="PAYLOAD_RELEASE_MISSING"
        selection_message="Selected external Runtime payload release is missing or unsafe."
        ;;
      *"public tool count is incompatible."*|*"public tool surface is incompatible."*|*"public tool contract is inconsistent."*)
        selection_reason="PAYLOAD_SUBSTRATE_INCOMPATIBLE"
        selection_message="Selected external Runtime payload is incompatible with the immutable substrate."
        ;;
    esac
    doctor_error "$selection_reason" "$selection_message" "$json_mode"
  fi
  IFS=$'\t' read -r selected_payload runtime_revision <<< "$selection"
  [[ -f "$selected_payload/agent_runtime/doctor.py" ]] \
    || doctor_error "PAYLOAD_SELECTION_INVALID" "Selected external Runtime doctor is unavailable." "$json_mode"

  exec /usr/bin/python3 - "$doctor_env" "$doctor_python" "$doctor_root" "$selected_payload" "$runtime_revision" "$RUNTIME_PATH" "$json_mode" "$doctor_arg" <<'PY'
import os
import sys
from pathlib import Path

env_file = Path(sys.argv[1])
runtime_python = sys.argv[2]
runtime_root = sys.argv[3]
selected_payload = sys.argv[4]
runtime_revision = sys.argv[5]
runtime_path = sys.argv[6]
json_mode = sys.argv[7] == "1"
doctor_arg = sys.argv[8]

sys.dont_write_bytecode = True
sys.path.insert(0, runtime_root)
from macos import runtime_config

def fail(reason, message):
    if json_mode:
        import json
        print(json.dumps({"error": {"message": message, "reason_code": reason}}, separators=(",", ":"), sort_keys=True))
    else:
        print(f"START ERROR: {message} ({reason})", file=sys.stderr)
    raise SystemExit(2)

try:
    _, _, values = runtime_config._read(env_file, require_mode=True)
    runtime_config._validate_values(values)
    runtime_config._validated_identity(values, required=True)
except SystemExit:
    fail("CANONICAL_CONFIG_INVALID", "Canonical runtime.env is malformed, unsafe, or incomplete.")

doctor_env = {
    "PATH": runtime_path,
    "HOME": os.environ.get("HOME", str(Path.home())),
    "PYTHONPATH": selected_payload,
    "PYTHONDONTWRITEBYTECODE": "1",
    "AGENT_RUNTIME_WORKSPACE_ROOT": values["AGENT_RUNTIME_WORKSPACE_ROOT"],
    "AGENT_RUNTIME_GIT_NAME": values["AGENT_RUNTIME_GIT_NAME"],
    "AGENT_RUNTIME_GIT_EMAIL": values["AGENT_RUNTIME_GIT_EMAIL"],
    "AGENT_RUNTIME_REVISION": runtime_revision,
}
for key in ("AGENT_RUNTIME_MAX_ACTIVE_SESSIONS", "AGENT_RUNTIME_MAX_PARALLELISM"):
    if key in values:
        doctor_env[key] = values[key]
doctor_env["AGENT_RUNTIME_TELEMETRY"] = values.get("AGENT_RUNTIME_TELEMETRY", "off")
for key in ("USER", "TMPDIR", "LANG"):
    value = os.environ.get(key)
    if value:
        doctor_env[key] = value
for key, value in os.environ.items():
    if key.startswith("LC_") and value:
        doctor_env[key] = value

argv = [runtime_python, "-m", "agent_runtime.doctor"]
if doctor_arg:
    argv.append(doctor_arg)
os.execve(runtime_python, argv, doctor_env)
PY
}

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

service_quiescent() {
  local state
  state="$(launchctl print "$SERVICE" 2>/dev/null)" || return 1
  [[ "$state" =~ (^|$'\n')[[:space:]]*state[[:space:]]*=[[:space:]]*(not[[:space:]]running|waiting)($|$'\n') ]] \
    || return 1
  [[ ! "$state" =~ (^|$'\n')[[:space:]]*pid[[:space:]]*=[[:space:]]*[0-9]+($|$'\n') ]]
}

effective_session_limit() {
  local value=""
  if [[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]]; then
    value="$(awk -F= '$1 == "AGENT_RUNTIME_MAX_ACTIVE_SESSIONS" { print substr($0, index($0, "=") + 1); exit }' "$ENV_FILE")"
  fi
  if [[ "$value" =~ ^[1-6]$ ]]; then
    printf '%s\n' "$value"
  else
    printf '%s\n' "$DEFAULT_SESSION_LIMIT"
  fi
}

port_owner_pids() {
  command -v lsof >/dev/null 2>&1 || fail "lsof is required to protect port 8080."
  lsof -nP -iTCP:8080 -sTCP:LISTEN -t 2>/dev/null | sort -u || true
}

is_canonical_port_owner() {
  local pid="$1" command executable
  command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  command="${command//\\ / }"
  executable="${command%% *}"
  [[ "${executable##*/}" == "tunnel-client" ]] || return 1
  [[ "$command" == *"tunnel-client run"* ]] || return 1
  [[ "$command" == *"--control-plane.poll-channel main"* ]] || return 1
  [[ "$command" == *"--mcp.command $MCP_COMMAND_NORMALIZED"* ]] || return 1
  [[ "$command" == *"--health.listen-addr 127.0.0.1:8080"* ]] || return 1
  [[ "$command" != *"--profile"* ]]
}

service_pid() {
  launchctl print "$SERVICE" 2>/dev/null \
    | awk '/^[[:space:]]*pid[[:space:]]*=[[:space:]]*[0-9]+[[:space:]]*$/ { print $3; exit }'
}

is_tunnel_client_process() {
  local pid="$1" executable
  executable="$(ps -p "$pid" -o comm= 2>/dev/null | awk 'NF { print; exit }')"
  [[ "${executable##*/}" == "tunnel-client" ]]
}

canonical_control_owner() {
  local pid="$1" supervisor parent
  (require_current_launchagent) >/dev/null 2>&1 || return 1
  supervisor="$(service_pid)"
  [[ "$supervisor" =~ ^[0-9]+$ ]] || return 1
  parent="$(ps -p "$pid" -o ppid= 2>/dev/null | awk 'NF { print $1; exit }')"
  [[ "$parent" == "$supervisor" ]] || return 1
  is_tunnel_client_process "$pid"
}

is_canonical_runtime_identity() {
  local pid="$1"
  canonical_control_owner "$pid" || is_canonical_port_owner "$pid"
}

preflight_protected_port() {
  local owners pid
  owners="$(port_owner_pids)"
  [[ -n "$owners" ]] || return 0
  service_loaded || fail "Protected port 8080 is occupied by an unsupervised or foreign process; refusing to kill or rebind it."
  while IFS= read -r pid; do
    [[ -n "$pid" ]] || continue
    is_canonical_runtime_identity "$pid" || fail "Protected port 8080 is occupied by a foreign or ambiguous process; refusing to kill or rebind it."
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
  if ! is_canonical_runtime_identity "$pid"; then
    READY_DIAGNOSTIC="port 8080 listener is not the canonical installed no-profile Runtime"
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

STATUS_STATE="attention"
STATUS_CONTROL="none"
STATUS_PIDS=""
STATUS_HEALTH="unverified"
STATUS_READY="unverified"
STATUS_TUNNEL_TRANSPORT="not-running"
STATUS_DETAIL="Runtime status has not been observed."

status_pid_json() {
  printf '%s\n' "$STATUS_PIDS" | awk '
    BEGIN { first=1; printf "[" }
    /^[0-9]+$/ { if (!first) printf ","; printf "%s", $1; first=0 }
    END { print "]" }
  '
}

observe_tunnel_transport() {
  local response body http_code schema component status
  STATUS_TUNNEL_TRANSPORT="unconfirmed"

  response="$(curl -sS --max-time 1 -w $'\n%{http_code}' "$HEALTH_URL/health/control-plane" 2>/dev/null)" || return 0
  http_code="${response##*$'\n'}"
  body="${response%$'\n'*}"
  [[ "$http_code" == "200" ]] || return 0
  [[ -n "$body" ]] || return 0
  [[ -x /usr/bin/plutil ]] || return 0

  schema="$(printf '%s' "$body" | /usr/bin/plutil -extract schema_version raw -o - - 2>/dev/null)" || return 0
  component="$(printf '%s' "$body" | /usr/bin/plutil -extract component raw -o - - 2>/dev/null)" || return 0
  status="$(printf '%s' "$body" | /usr/bin/plutil -extract status raw -o - - 2>/dev/null)" || return 0
  [[ "$schema" == "1" && "$component" == "control-plane" ]] || return 0

  case "$status" in
    ok)
      STATUS_TUNNEL_TRANSPORT="healthy"
      ;;
    degraded)
      STATUS_TUNNEL_TRANSPORT="degraded"
      ;;
    unknown|disabled|unobserved)
      STATUS_TUNNEL_TRANSPORT="unconfirmed"
      ;;
    *)
      STATUS_TUNNEL_TRANSPORT="unconfirmed"
      ;;
  esac
}

observe_runtime_status() {
  local owners count pid
  STATUS_STATE="attention"
  STATUS_CONTROL="none"
  STATUS_PIDS=""
  STATUS_HEALTH="unverified"
  STATUS_READY="unverified"
  STATUS_TUNNEL_TRANSPORT="not-running"
  STATUS_DETAIL="Runtime status is unavailable."

  owners="$(port_owner_pids)"
  STATUS_PIDS="$owners"
  count="$(printf '%s\n' "$owners" | awk 'NF { count++ } END { print count+0 }')"
  if [[ "$count" == "0" ]]; then
    STATUS_STATE="stopped"
    STATUS_DETAIL="No Runtime listener is serving."
    return 0
  fi
  if [[ "$count" != "1" ]]; then
    STATUS_DETAIL="Multiple listeners occupy the protected Runtime port; ownership is ambiguous."
    return 0
  fi

  pid="$(printf '%s\n' "$owners" | awk 'NF { print; exit }')"
  if ! is_canonical_runtime_identity "$pid"; then
    STATUS_DETAIL="Port 8080 is occupied by a foreign or ambiguous process."
    return 0
  fi
  command -v curl >/dev/null 2>&1 || fail "curl is required for Runtime readiness checks."
  if curl -fsS --max-time 1 "$HEALTH_URL/healthz" >/dev/null 2>&1; then
    STATUS_HEALTH="live"
  else
    STATUS_HEALTH="failed"
    STATUS_DETAIL="Canonical Runtime identity is present, but healthz is not green."
    return 0
  fi
  if curl -fsS --max-time 1 "$HEALTH_URL/readyz" >/dev/null 2>&1; then
    STATUS_READY="ready"
  else
    STATUS_READY="failed"
    STATUS_DETAIL="Canonical Runtime identity is present, but readyz is not green."
    return 0
  fi

  STATUS_STATE="running"
  STATUS_DETAIL="Canonical Runtime is serving and ready."
  if canonical_control_owner "$pid"; then
    STATUS_CONTROL="managed"
  else
    STATUS_CONTROL="read-only"
  fi
  observe_tunnel_transport
}

require_lifecycle_control_authority() {
  local owners count pid
  owners="$(port_owner_pids)"
  count="$(printf '%s\n' "$owners" | awk 'NF { count++ } END { print count+0 }')"
  if [[ "$count" == "0" ]]; then
    if service_loaded; then
      require_current_launchagent
    fi
    return 0
  fi
  [[ "$count" == "1" ]] \
    || fail "Runtime lifecycle control is unavailable because protected-port ownership is ambiguous."
  pid="$(printf '%s\n' "$owners" | awk 'NF { print; exit }')"
  canonical_control_owner "$pid" \
    || fail "Runtime is serving without positive canonical lifecycle-control ownership; refusing to signal it."
}

emit_runtime_status() {
  local json_mode="${1:-0}" pids_json
  observe_runtime_status
  pids_json="$(status_pid_json)"
  if [[ "$json_mode" == "1" ]]; then
    printf '{"schema":3,"state":"%s","control":"%s","pids":%s,"health":"%s","ready":"%s","tunnel_transport":"%s","detail":"%s"}\n' \
      "$STATUS_STATE" "$STATUS_CONTROL" "$pids_json" "$STATUS_HEALTH" "$STATUS_READY" "$STATUS_TUNNEL_TRANSPORT" "$STATUS_DETAIL"
    return 0
  fi
  case "$STATUS_STATE/$STATUS_CONTROL" in
    running/managed)
      echo "RUNNING (managed; tunnel: $STATUS_TUNNEL_TRANSPORT; persistent terminal sessions: $(effective_session_limit))"
      ;;
    running/read-only)
      echo "RUNNING (read-only/external; tunnel: $STATUS_TUNNEL_TRANSPORT; persistent terminal sessions: $(effective_session_limit))"
      ;;
    stopped/*)
      echo "STOPPED (persistent terminal sessions: $(effective_session_limit))"
      ;;
    *)
      echo "ATTENTION: $STATUS_DETAIL (persistent terminal sessions: $(effective_session_limit))"
      ;;
  esac
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
  for i in {1..70}; do
    owners="$(port_owner_pids)"
    if [[ -z "$owners" ]] && (! service_loaded || service_quiescent); then
      return 0
    fi
    while IFS= read -r pid; do
      [[ -n "$pid" ]] || continue
      is_canonical_runtime_identity "$pid" \
        || fail "Port 8080 changed to a foreign or ambiguous owner while stopping; refusing to claim Runtime is stopped."
    done <<< "$owners"
    sleep 0.1
  done
  fail "Canonical Runtime supervisor or listener did not stop within the bounded shutdown window."
}

serve() {
  local tunnel_client="${1:-}"
  local env_file="${2:-$ENV_FILE}"
  local require_git_identity=0
  local selected_payload="$ROOT"
  local runtime_revision=""

  [[ "$SOURCE_ROOT" != "$INSTALLED_RUNTIME_ROOT" || "$env_file" == "$CANONICAL_ENV_FILE" ]] \
    || fail "Installed Runtime configuration must use the canonical per-user file."
  [[ -f "$env_file" && ! -L "$env_file" ]] \
    || fail "Missing or invalid canonical Runtime configuration; run ./install.sh first."
  if [[ "$SOURCE_ROOT" == "$INSTALLED_RUNTIME_ROOT" ]]; then
    require_git_identity=1
    local selection
    selection="$(validate_installed_selection "$ROOT" "$RUNTIME_PYTHON")"
    IFS=$'\t' read -r selected_payload runtime_revision <<< "$selection"
    /usr/bin/python3 - "$env_file" <<'PY'
import stat
import sys
from pathlib import Path
path = Path(sys.argv[1])
try:
    mode = stat.S_IMODE(path.stat().st_mode)
except OSError as exc:
    raise SystemExit("START ERROR: could not inspect Runtime configuration: " + str(exc))
if mode != 0o600:
    raise SystemExit("START ERROR: Runtime configuration must have mode 0600.")
PY
  fi
  [[ ! -e "$LEGACY_CONFIG" && ! -L "$LEGACY_CONFIG" ]] \
    || fail "Legacy tunnel configuration must remain absent at $LEGACY_CONFIG."
  [[ "$tunnel_client" == /* && -x "$tunnel_client" && "${tunnel_client##*/}" == "tunnel-client" ]] \
    || fail "LaunchAgent must provide an absolute executable tunnel-client path."
  [[ -x "$RUNTIME_PYTHON" && -f "$selected_payload/agent_runtime/server.py" ]] \
    || fail "Selected Runtime payload is incomplete."

  exec /usr/bin/python3 - "$env_file" "$tunnel_client" "$RUNTIME_PYTHON" "$RUNTIME_PATH" "$selected_payload" "$runtime_revision" "$require_git_identity" <<'PY'
import os
import re
import subprocess
import sys
from pathlib import Path

env_file = Path(sys.argv[1])
tunnel_client = sys.argv[2]
runtime_python = sys.argv[3]
runtime_path = sys.argv[4]
selected_payload = sys.argv[5]
runtime_revision = sys.argv[6]
require_git_identity = sys.argv[7] == "1"
sys.dont_write_bytecode = True

def fail(message):
    print("START ERROR: " + message, file=sys.stderr)
    raise SystemExit(2)

try:
    lines = env_file.read_text(encoding="utf-8").splitlines()
except OSError as exc:
    fail("Could not read Runtime configuration: " + str(exc))

required = {
    "CONTROL_PLANE_API_KEY",
    "CONTROL_PLANE_TUNNEL_ID",
    "AGENT_RUNTIME_WORKSPACE_ROOT",
}
git_identity_keys = ("AGENT_RUNTIME_GIT_NAME", "AGENT_RUNTIME_GIT_EMAIL")
optional = {"AGENT_RUNTIME_MAX_ACTIVE_SESSIONS", "AGENT_RUNTIME_MAX_PARALLELISM", "AGENT_RUNTIME_TELEMETRY"}
values = {}
for number, line in enumerate(lines, start=1):
    if not line or line.lstrip().startswith("#"):
        continue
    match = re.fullmatch(r"([A-Z_][A-Z0-9_]*)=(.*)", line)
    if match is None:
        fail("Malformed Runtime configuration entry at line " + str(number) + ".")
    key, value = match.groups()
    if key in required or key in optional or key in git_identity_keys:
        if key in values:
            fail("Duplicate " + key + " entry in Runtime configuration.")
        values[key] = value

missing = sorted(key for key in required if not values.get(key, ""))
identity_required = require_git_identity or any(key in values for key in git_identity_keys)
if identity_required:
    missing.extend(key for key in git_identity_keys if not values.get(key, ""))
if missing:
    fail("Missing non-empty Runtime configuration value for " + ", ".join(missing) + ".")
workspace = Path(values["AGENT_RUNTIME_WORKSPACE_ROOT"])
if not workspace.is_absolute() or not workspace.is_dir():
    fail("AGENT_RUNTIME_WORKSPACE_ROOT must be an absolute existing directory.")
parallelism = values.get("AGENT_RUNTIME_MAX_PARALLELISM")
if parallelism is not None and (re.fullmatch(r"[1-9][0-9]*", parallelism) is None or not 1 <= int(parallelism) <= 10):
    fail("AGENT_RUNTIME_MAX_PARALLELISM must be an integer from 1 through 10.")
session_limit = values.get("AGENT_RUNTIME_MAX_ACTIVE_SESSIONS")
if session_limit is not None and (re.fullmatch(r"[1-9][0-9]*", session_limit) is None or not 1 <= int(session_limit) <= 6):
    fail("AGENT_RUNTIME_MAX_ACTIVE_SESSIONS must be an integer from 1 through 6.")
telemetry_mode = values.get("AGENT_RUNTIME_TELEMETRY", "off")
if telemetry_mode not in {"off", "otlp"}:
    fail("AGENT_RUNTIME_TELEMETRY must be off or otlp.")
if identity_required:
    for key in git_identity_keys:
        value = values[key]
        try:
            raw = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            fail("Runtime Git identity is malformed.")
        if len(raw) > 256 or any(ch in value for ch in ("\x00", "\r", "\n")):
            fail("Runtime Git identity is malformed.")

runtime_env = {
    "PATH": runtime_path,
    "HOME": os.environ.get("HOME", str(Path.home())),
    "CONTROL_PLANE_API_KEY": values["CONTROL_PLANE_API_KEY"],
    "CONTROL_PLANE_TUNNEL_ID": values["CONTROL_PLANE_TUNNEL_ID"],
    "AGENT_RUNTIME_WORKSPACE_ROOT": values["AGENT_RUNTIME_WORKSPACE_ROOT"],
    "PYTHONPATH": selected_payload,
    "PYTHONDONTWRITEBYTECODE": "1",
    "OPEN_WEB_UI": "false",
    "AGENT_RUNTIME_TELEMETRY": telemetry_mode,
}
if identity_required:
    for key in git_identity_keys:
        runtime_env[key] = values[key]
if values.get("AGENT_RUNTIME_MAX_ACTIVE_SESSIONS") is not None:
    runtime_env["AGENT_RUNTIME_MAX_ACTIVE_SESSIONS"] = values["AGENT_RUNTIME_MAX_ACTIVE_SESSIONS"]
if values.get("AGENT_RUNTIME_MAX_PARALLELISM") is not None:
    runtime_env["AGENT_RUNTIME_MAX_PARALLELISM"] = values["AGENT_RUNTIME_MAX_PARALLELISM"]
if runtime_revision:
    runtime_env["AGENT_RUNTIME_REVISION"] = runtime_revision
for key in ("USER", "TMPDIR", "LANG"):
    value = os.environ.get(key)
    if value:
        runtime_env[key] = value
for key, value in os.environ.items():
    if key.startswith("LC_") and value:
        runtime_env[key] = value

common = [
    "--control-plane.poll-channel", "main",
    "--mcp.command", "command=" + runtime_python.replace(" ", "\\ ") + " -m agent_runtime.server,channel=main",
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

validate_current_runtime_before_start() {
  require_current_launchagent
  if [[ "$SOURCE_ROOT" == "$INSTALLED_RUNTIME_ROOT" ]]; then
    validate_installed_selection "$ROOT" "$RUNTIME_PYTHON" >/dev/null
  fi
}

case "$ACTION" in
  --help|-h)
    cat <<'EOF'
Usage: ./start.sh [start|stop|restart|status [--json]|session-limit|doctor [--json]]
The installed Runtime uses one exact per-user LaunchAgent and one validated external payload selection.
--serve is an internal supervisor entrypoint and is not an operator command.
EOF
    ;;
  doctor)
    shift
    installed_doctor "$@"
    ;;
  --serve)
    serve "${2:-}" "${3:-$ENV_FILE}"
    ;;
  start)
    acquire_lock
    validate_current_runtime_before_start
    preflight_protected_port
    if runtime_ready_once; then
      echo "Agent Runtime: RUNNING"
      exit 0
    fi
    launchctl kickstart "$SERVICE" >/dev/null 2>&1 || fail "Could not start canonical Runtime service."
    wait_until_ready
    echo "Agent Runtime: RUNNING"
    ;;
  stop)
    acquire_lock
    require_lifecycle_control_authority
    if service_loaded; then
      launchctl kill SIGTERM "$SERVICE" >/dev/null 2>&1 || true
    fi
    wait_until_stopped
    echo "Agent Runtime: STOPPED"
    ;;
  restart)
    acquire_lock
    validate_current_runtime_before_start
    preflight_protected_port
    require_lifecycle_control_authority
    if service_loaded; then
      launchctl kill SIGTERM "$SERVICE" >/dev/null 2>&1 || true
      wait_until_stopped
    fi
    launchctl kickstart "$SERVICE" >/dev/null 2>&1 || fail "Could not restart canonical Runtime service."
    wait_until_ready
    echo "Agent Runtime: RUNNING"
    ;;
  status)
    if [[ "$SOURCE_ROOT" == "$INSTALLED_RUNTIME_ROOT" ]]; then
      validate_installed_selection "$ROOT" "$RUNTIME_PYTHON" >/dev/null
    fi
    if [[ "${2:-}" == "--json" ]]; then
      [[ "$#" == "2" ]] || fail "Usage: ./start.sh status [--json]"
      emit_runtime_status 1
    elif [[ "$#" == "1" ]]; then
      emit_runtime_status 0
    else
      fail "Usage: ./start.sh status [--json]"
    fi
    ;;
  session-limit)
    echo "AGENT_RUNTIME_MAX_ACTIVE_SESSIONS effective: $(effective_session_limit)"
    ;;
  *)
    fail "Usage: ./start.sh [start|stop|restart|status [--json]|session-limit|doctor [--json]]"
    ;;
esac
