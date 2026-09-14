#!/usr/bin/env bash
set -euo pipefail

fail() {
  echo "INSTALL ERROR: $*" >&2
  exit 2
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$ROOT"

run_cutover_helper() {
  command -v python3 >/dev/null 2>&1 || fail "Python 3.11+ is required for candidate cutover."
  exec "$(command -v python3)" "$ROOT/macos/candidate_cutover.py" "$@"
}

case "${1-}" in
  --install-prebuilt)
    [[ "$#" == "3" ]] || fail "usage: ./install.sh --install-prebuilt <Agent Runtime.app> <candidate.json>"
    [[ "$(uname -s)" == "Darwin" ]] || fail "prebuilt candidate installation supports macOS only."
    command -v launchctl >/dev/null 2>&1 || fail "launchctl is required for candidate cutover."
    command -v tunnel-client >/dev/null 2>&1 || fail "tunnel-client is required for candidate cutover."
    run_cutover_helper cutover "$2" "$3" --home "$HOME" \
      --launchctl "$(command -v launchctl)" --tunnel-client "$(command -v tunnel-client)"
    ;;
  --commit-cutover)
    [[ "$#" == "1" ]] || fail "usage: ./install.sh --commit-cutover"
    run_cutover_helper commit --home "$HOME"
    ;;
  --rollback-cutover)
    [[ "$#" == "1" ]] || fail "usage: ./install.sh --rollback-cutover"
    [[ "$(uname -s)" == "Darwin" ]] || fail "candidate rollback supports macOS only."
    command -v launchctl >/dev/null 2>&1 || fail "launchctl is required for candidate rollback."
    run_cutover_helper rollback --home "$HOME" --launchctl "$(command -v launchctl)"
    ;;
esac

if [[ "${1-}" == "--recover-runtime-service" ]]; then
  [[ "$(uname -s)" == "Darwin" ]] || fail "Runtime service recovery supports macOS only."
  command -v python3 >/dev/null 2>&1 || fail "Python 3.11+ is required for Runtime service recovery."
  command -v launchctl >/dev/null 2>&1 || fail "launchctl is required for Runtime service recovery."
  shift
  exec python3 "$ROOT/macos/recover_runtime_service.py" \
    --repository-root "$ROOT" \
    --canonical-root "$ROOT" \
    --expected-tunnel-fingerprint "6aa2b81d6dd8" \
    --launchctl "$(command -v launchctl)" \
    "$@"
fi

[[ "$(uname -s)" == "Darwin" ]] || fail "agent-runtime install.sh supports macOS only."
command -v git >/dev/null 2>&1 || fail "git is required."
source "$ROOT/macos/packaging_python.sh"
PACKAGING_PYTHON="$(resolve_packaging_python "INSTALL ERROR")"
command -v tunnel-client >/dev/null 2>&1 || fail "tunnel-client is required; install the official OpenAI tunnel-client first."
command -v xcrun >/dev/null 2>&1 || fail "Xcode command-line tools are required for the native menu-bar app."
command -v launchctl >/dev/null 2>&1 || fail "launchctl is required for native Runtime supervision."
xcrun --find swift >/dev/null 2>&1 || fail "Swift is required for the native menu-bar app."
TUNNEL_CLIENT="$(command -v tunnel-client)"
LAUNCHCTL="$(command -v launchctl)"

GIT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
[[ -n "$GIT_ROOT" ]] || fail "run install.sh from a Git clone of agent-runtime."
GIT_ROOT="$(cd "$GIT_ROOT" && pwd -P)"
[[ "$GIT_ROOT" == "$ROOT" ]] || fail "install.sh must run from the agent-runtime checkout root."

REMOTE="$(git config --get remote.origin.url || true)"
case "$REMOTE" in
  https://github.com/phatnguyen03022001/agent-runtime.git|git@github.com:phatnguyen03022001/agent-runtime.git) ;;
  *) fail "origin must identify phatnguyen03022001/agent-runtime exactly." ;;
esac

WORKSPACE_ROOT="$(dirname "$ROOT")"
[[ "$WORKSPACE_ROOT" == /* && -d "$WORKSPACE_ROOT" ]] || fail "derived workspace root must be an absolute existing directory."

ENV_FILE="$ROOT/.env"
CANONICAL_ENV_FILE="$HOME/Library/Application Support/Agent Runtime/runtime.env"
LEGACY_CONFIG="$HOME/.config/tunnel-client/agent-runtime.yaml"
RUNTIME_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
[[ ! -e "$LEGACY_CONFIG" && ! -L "$LEGACY_CONFIG" ]] \
  || fail "Legacy tunnel configuration must remain absent at $LEGACY_CONFIG."
[[ "$TUNNEL_CLIENT" == /* && -f "$TUNNEL_CLIENT" && -x "$TUNNEL_CLIENT" ]] \
  || fail "resolved tunnel-client must be an absolute executable."

if [[ ! -e "$ROOT/.venv" ]]; then
  echo "[1/8] Creating local Python environment..."
  "$PACKAGING_PYTHON" -m venv "$ROOT/.venv"
fi
[[ -d "$ROOT/.venv" && ! -L "$ROOT/.venv" && -x "$ROOT/.venv/bin/python" ]] \
  || fail "existing .venv is not a usable local virtual environment."
validate_packaging_python "$ROOT/.venv/bin/python" "INSTALL ERROR"

"$ROOT/.venv/bin/python" -m pip install --require-hashes -r "$ROOT/requirements.lock"
PYTHON="$ROOT/.venv/bin/python" "$ROOT/verify"

if [[ -e "$ENV_FILE" || -L "$ENV_FILE" ]]; then
  [[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || fail "existing .env is not a regular file."
else
  echo "[2/8] Creating ignored local environment file..."
  cp "$ROOT/.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
fi

echo "[3/8] Initializing canonical Runtime configuration..."
/usr/bin/python3 "$ROOT/macos/runtime_config.py" "$ENV_FILE" "$CANONICAL_ENV_FILE" "$WORKSPACE_ROOT"
ENV_FILE="$CANONICAL_ENV_FILE"
echo "Validating canonical Runtime configuration..."
INSTALL_API_KEY="${CONTROL_PLANE_API_KEY-}" \
INSTALL_TUNNEL_ID="${CONTROL_PLANE_TUNNEL_ID-}" \
/usr/bin/python3 - "$ENV_FILE" "$TUNNEL_CLIENT" "$ROOT/.venv/bin/python" "$WORKSPACE_ROOT" "$HOME" "$RUNTIME_PATH" "$LAUNCHCTL" <<'PY'
import os
import re
import subprocess
import sys
from pathlib import Path

env_file = Path(sys.argv[1])
tunnel_client = sys.argv[2]
runtime_python = sys.argv[3]
workspace_root = sys.argv[4]
home = sys.argv[5]
runtime_path = sys.argv[6]
launchctl = sys.argv[7]

def fail(message):
    print("INSTALL ERROR: " + message, file=sys.stderr)
    raise SystemExit(2)

try:
    lines = env_file.read_text(encoding="utf-8").splitlines()
except OSError as exc:
    fail("Could not read checkout-local .env: " + str(exc))

required = ("CONTROL_PLANE_API_KEY", "CONTROL_PLANE_TUNNEL_ID", "AGENT_RUNTIME_WORKSPACE_ROOT")
optional = {"AGENT_RUNTIME_MAX_ACTIVE_SESSIONS"}
values = {}
other = []
for number, line in enumerate(lines, start=1):
    if not line or line.lstrip().startswith("#"):
        other.append(line)
        continue
    match = re.fullmatch(r"([A-Z_][A-Z0-9_]*)=(.*)", line)
    if match is None:
        fail("Malformed .env entry at line " + str(number) + ".")
    key, value = match.groups()
    if key in required or key in optional:
        if key in values:
            fail("Duplicate " + key + " entry in .env.")
        values[key] = value
    else:
        other.append(line)

for key, inherited in (("CONTROL_PLANE_API_KEY", os.environ.get("INSTALL_API_KEY", "")),
                       ("CONTROL_PLANE_TUNNEL_ID", os.environ.get("INSTALL_TUNNEL_ID", ""))):
    if not values.get(key, "") and inherited:
        values[key] = inherited
values["AGENT_RUNTIME_WORKSPACE_ROOT"] = workspace_root
missing = [key for key in required if not values.get(key, "")]
if missing:
    fail("Missing non-empty .env value for " + ", ".join(missing) + ".")

runtime_env = {
    "PATH": runtime_path,
    "HOME": home,
    "CONTROL_PLANE_API_KEY": values["CONTROL_PLANE_API_KEY"],
    "CONTROL_PLANE_TUNNEL_ID": values["CONTROL_PLANE_TUNNEL_ID"],
    "AGENT_RUNTIME_WORKSPACE_ROOT": values["AGENT_RUNTIME_WORKSPACE_ROOT"],
    "PYTHONPATH": str(env_file.parent),
    "OPEN_WEB_UI": "false",
}
common = [
    "--control-plane.poll-channel", "main",
    "--mcp.command", "command=" + runtime_python + " -m agent_runtime.server,channel=main",
    "--health.listen-addr", "127.0.0.1:8080",
]
check = subprocess.run(
    [tunnel_client, "doctor"] + common + ["--explain"],
    env=runtime_env,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    check=False,
)
if check.returncode != 0:
    detail = check.stdout + "\n" + check.stderr
    occupied_listener = "health_listener" in detail and "address already in use" in detail
    service = "gui/" + str(os.getuid()) + "/com.picmao.agent-runtime-runtime"
    service_loaded = subprocess.run(
        [launchctl, "print", service],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0
    owners = subprocess.run(
        ["/usr/sbin/lsof", "-nP", "-iTCP:8080", "-sTCP:LISTEN", "-t"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    ).stdout.split()
    existing_is_canonical = False
    if len(owners) == 1:
        process = subprocess.run(
            ["/bin/ps", "-p", owners[0], "-o", "command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        command = process.stdout.strip().replace("\\ ", " ")
        executable = command.split(" ", 1)[0] if command else ""
        existing_is_canonical = (
            Path(executable).name == "tunnel-client"
            and ("tunnel-client" + " run") in command
            and "--health.listen-addr 127.0.0.1:8080" in command
            and "--profile" not in command
        )
    healthy = all(
        subprocess.run(
            ["/usr/bin/curl", "-fsS", "--max-time", "1", "http://127.0.0.1:8080/" + endpoint],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
        for endpoint in ("healthz", "readyz")
    )
    if not (occupied_listener and service_loaded and existing_is_canonical and healthy):
        fail("tunnel-client configuration check failed; Runtime was not started.")

PY

echo "[4/8] Building package-owned Runtime payload and menu-bar app..."
"$ROOT/macos/package_app.sh" >/dev/null
SOURCE_APP="$ROOT/build/Agent Runtime.app"
CANDIDATE_HANDOFF="$ROOT/build/Agent Runtime.candidate.json"
[[ -d "$SOURCE_APP" && ! -L "$SOURCE_APP" ]] || fail "packaged app candidate is missing or unsafe."
[[ -f "$CANDIDATE_HANDOFF" && ! -L "$CANDIDATE_HANDOFF" ]] || fail "external candidate handoff is missing or unsafe."

echo "[5/8] Installing the sealed prebuilt candidate transactionally..."
"$ROOT/install.sh" --install-prebuilt "$SOURCE_APP" "$CANDIDATE_HANDOFF"

echo "[6/8] Prebuilt candidate is installed and pending explicit commit."
TARGET_APP="$HOME/Applications/Agent Runtime.app"
RUNTIME_ROOT="$TARGET_APP/Contents/Resources/runtime"
/usr/bin/python3 "$ROOT/macos/package_provenance.py" validate-candidate "$TARGET_APP" "$CANDIDATE_HANDOFF" \
  || fail "installed candidate changed before commit."

echo "[7/8] Candidate integrity verified; rollback remains available."

echo "[8/8] Cutover is pending explicit commit after downstream live acceptance."
echo "Workspace root: $WORKSPACE_ROOT"
echo "Tunnel authority: per-user Application Support runtime.env"
echo "Native app: $TARGET_APP"
echo "Installed Runtime payload: $RUNTIME_ROOT"
echo "Login behavior: menu-bar UI is registered now; Runtime follows explicit persisted desired state."
SESSION_LIMIT_VALUE="$(awk -F= '$1 == "AGENT_RUNTIME_MAX_ACTIVE_SESSIONS" { print substr($0, index($0, "=") + 1); exit }' "$ENV_FILE")"
if [[ "$SESSION_LIMIT_VALUE" =~ ^[1-9][0-9]*$ ]]; then
  SESSION_LIMIT_EFFECTIVE="$SESSION_LIMIT_VALUE"
else
  SESSION_LIMIT_EFFECTIVE=64
fi
echo "Effective persistent terminal sessions: $SESSION_LIMIT_EFFECTIVE"
echo "Open Agent Runtime.app and press Start, or use ./start.sh start as the operator CLI fallback."
