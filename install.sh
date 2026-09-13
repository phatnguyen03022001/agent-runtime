#!/usr/bin/env bash
set -euo pipefail

fail() {
  echo "INSTALL ERROR: $*" >&2
  exit 2
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$ROOT"

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
command -v python3 >/dev/null 2>&1 || fail "Python 3.11+ is required."
command -v tunnel-client >/dev/null 2>&1 || fail "tunnel-client is required; install the official OpenAI tunnel-client first."
command -v xcrun >/dev/null 2>&1 || fail "Xcode command-line tools are required for the native menu-bar app."
command -v launchctl >/dev/null 2>&1 || fail "launchctl is required for native Runtime supervision."
xcrun --find swift >/dev/null 2>&1 || fail "Swift is required for the native menu-bar app."
TUNNEL_CLIENT="$(command -v tunnel-client)"
LAUNCHCTL="$(command -v launchctl)"

python3 - <<'PY' || exit 2
import sys
if sys.version_info < (3, 11):
    raise SystemExit("INSTALL ERROR: Python 3.11+ is required.")
PY

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
  python3 -m venv "$ROOT/.venv"
fi
[[ -d "$ROOT/.venv" && ! -L "$ROOT/.venv" && -x "$ROOT/.venv/bin/python" ]] \
  || fail "existing .venv is not a usable local virtual environment."

"$ROOT/.venv/bin/python" -m pip install -r "$ROOT/requirements.txt"
PYTHON="$ROOT/.venv/bin/python" "$ROOT/verify"

if [[ -e "$ENV_FILE" || -L "$ENV_FILE" ]]; then
  [[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || fail "existing .env is not a regular file."
else
  echo "[2/8] Creating ignored local environment file..."
  cp "$ROOT/.env.example" "$ENV_FILE"
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
TARGET_APPS="$HOME/Applications"
TARGET_APP="$TARGET_APPS/Agent Runtime.app"
mkdir -p "$TARGET_APPS"

validate_package() {
  local app="$1"
  [[ -d "$app" && ! -L "$app" ]] || fail "package staging is not a regular app bundle."
  [[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$app/Contents/Info.plist" 2>/dev/null || true)" == "com.picmao.agent-runtime" ]] \
    || fail "package CFBundleIdentifier is not owned by agent-runtime."
  /usr/bin/codesign --verify --deep --strict "$app" \
    || fail "package failed strict deep code-signature verification."
/usr/bin/python3 - "$app" "$ROOT" <<'PY'
import hashlib
import json
import os
import sys
from pathlib import Path

app = Path(sys.argv[1])
checkout_root = Path(sys.argv[2]).resolve()
resources = app / "Contents/Resources"
manifest_path = resources / "runtime-manifest.json"
if manifest_path.is_symlink() or not manifest_path.is_file():
    raise SystemExit("INSTALL ERROR: package manifest is missing or symlinked")
try:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
except Exception as exc:
    raise SystemExit("INSTALL ERROR: package manifest is invalid") from exc
if manifest.get("schema") != 1 or manifest.get("owner") != "com.picmao.agent-runtime":
    raise SystemExit("INSTALL ERROR: package manifest ownership/version is invalid")
if not isinstance(manifest.get("runtime_revision"), str) or len(manifest["runtime_revision"]) != 40:
    raise SystemExit("INSTALL ERROR: package manifest revision is invalid")
if manifest.get("entrypoint") != "runtime/start.sh" or manifest.get("python") != "runtime/.venv/bin/python":
    raise SystemExit("INSTALL ERROR: package manifest execution paths are invalid")
runtime = resources / "runtime"
for relative in ("start.sh", ".venv/bin/python", "agent_runtime/server.py"):
    candidate = runtime / relative
    if candidate.is_symlink() or not candidate.is_file() or not os.access(candidate, os.X_OK if relative != "agent_runtime/server.py" else os.R_OK):
        raise SystemExit("INSTALL ERROR: package Runtime payload is incomplete")
for relative, manifest_key in (("start.sh", "start_sha256"), ("agent_runtime/server.py", "server_sha256")):
    candidate = runtime / relative
    expected = manifest.get(manifest_key)
    actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
    if expected != actual:
        raise SystemExit("INSTALL ERROR: package Runtime payload hash does not match its manifest")
for candidate in app.rglob("*"):
    if not candidate.is_file():
        continue
    if candidate.is_symlink():
        raise SystemExit("INSTALL ERROR: package contains a symlinked execution/resource file")
    try:
        payload = candidate.read_bytes()
    except OSError as exc:
        raise SystemExit("INSTALL ERROR: package file cannot be inspected") from exc
    if (b"env-" + b"path.txt") in payload or str(checkout_root).encode("utf-8") in payload:
        raise SystemExit("INSTALL ERROR: package contains a source-checkout implementation reference")
PY
}

if [[ -L "$TARGET_APP" ]]; then
  fail "existing $TARGET_APP must not be a symlink."
fi
STAGING_APP="$TARGET_APPS/.Agent Runtime.app.$$.staging"
BACKUP_APP="$TARGET_APPS/.Agent Runtime.app.$$.previous"
rm -rf "$STAGING_APP" "$BACKUP_APP"
/usr/bin/ditto "$SOURCE_APP" "$STAGING_APP"
validate_package "$STAGING_APP"
if [[ -e "$TARGET_APP" ]]; then
  EXISTING_ID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$TARGET_APP/Contents/Info.plist" 2>/dev/null || true)"
  [[ "$EXISTING_ID" == "com.picmao.agent-runtime" ]] || fail "existing $TARGET_APP is not owned by agent-runtime."
  mv "$TARGET_APP" "$BACKUP_APP"
fi
if ! mv "$STAGING_APP" "$TARGET_APP"; then
  if [[ -e "$BACKUP_APP" ]]; then mv "$BACKUP_APP" "$TARGET_APP"; fi
  fail "atomic app-bundle activation failed."
fi
rm -rf "$BACKUP_APP"
validate_package "$TARGET_APP"

echo "[5/8] Installing and immediately registering the UI login agent..."
LOGIN_DIR="$HOME/Library/LaunchAgents"
LOGIN_PLIST="$LOGIN_DIR/com.picmao.agent-runtime-ui.plist"
UI_LABEL="com.picmao.agent-runtime-ui"
UI_SERVICE="gui/$(id -u)/$UI_LABEL"
mkdir -p "$LOGIN_DIR"
if [[ -L "$LOGIN_PLIST" ]]; then
  fail "existing login launch configuration must not be a symlink."
fi
if [[ -e "$LOGIN_PLIST" && ! -f "$LOGIN_PLIST" ]]; then
  fail "existing login launch configuration must be a regular file."
fi
cat > "$LOGIN_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$UI_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$TARGET_APP/Contents/MacOS/AgentRuntimeMenuBar</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>ProcessType</key>
    <string>Interactive</string>
</dict>
</plist>
PLIST
/usr/bin/plutil -lint "$LOGIN_PLIST" >/dev/null || fail "login launch configuration is invalid."
if "$LAUNCHCTL" print "$UI_SERVICE" >/dev/null 2>&1; then
  # The label is already ours; kickstart refreshes the newly activated bundle
  # without registering a second job. The fallback accepts an already-loaded
  # UI on launchctl variants without kickstart support.
  "$LAUNCHCTL" kickstart -k "$UI_SERVICE" >/dev/null 2>&1 \
    || "$LAUNCHCTL" print "$UI_SERVICE" >/dev/null 2>&1 \
    || fail "could not refresh the existing menu-bar LaunchAgent."
else
  "$LAUNCHCTL" bootstrap "gui/$(id -u)" "$LOGIN_PLIST" >/dev/null 2>&1 \
    || "$LAUNCHCTL" print "$UI_SERVICE" >/dev/null 2>&1 \
    || fail "could not register the menu-bar LaunchAgent in the current login session."
fi
"$LAUNCHCTL" print "$UI_SERVICE" >/dev/null 2>&1 \
  || fail "menu-bar LaunchAgent is not loaded after installation."

echo "[6/8] Installing the package-owned protected Runtime LaunchAgent..."
RUNTIME_LABEL="com.picmao.agent-runtime-runtime"
RUNTIME_PLIST="$LOGIN_DIR/$RUNTIME_LABEL.plist"
STATE_DIR="$HOME/Library/Application Support/Agent Runtime"
DESIRED_STATE="$STATE_DIR/protected-runtime-running"
RUNTIME_ROOT="$TARGET_APP/Contents/Resources/runtime"
mkdir -p "$STATE_DIR"
DESIRED_STATE_WAS_PRESENT=0
if [[ -e "$DESIRED_STATE" ]]; then
  DESIRED_STATE_WAS_PRESENT=1
fi
if [[ -L "$RUNTIME_PLIST" ]]; then
  fail "existing Runtime launch configuration must not be a symlink."
fi
if [[ -e "$RUNTIME_PLIST" && ! -f "$RUNTIME_PLIST" ]]; then
  fail "existing Runtime launch configuration must be a regular file."
fi
cat > "$RUNTIME_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$RUNTIME_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$RUNTIME_ROOT/start.sh</string>
        <string>--serve</string>
        <string>$TUNNEL_CLIENT</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>$HOME</string>
        <key>PATH</key>
        <string>$RUNTIME_PATH</string>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>KeepAlive</key>
    <dict>
        <key>PathState</key>
        <dict>
            <key>$DESIRED_STATE</key>
            <true/>
        </dict>
    </dict>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>ThrottleInterval</key>
    <integer>2</integer>
</dict>
</plist>
PLIST
/usr/bin/plutil -lint "$RUNTIME_PLIST" >/dev/null || fail "Runtime launch configuration is invalid."
RUNTIME_SERVICE="gui/$(id -u)/$RUNTIME_LABEL"
if "$LAUNCHCTL" print "$RUNTIME_SERVICE" >/dev/null 2>&1; then
  # Updating ProgramArguments requires one bounded unregister/register cycle.
  # The desired-state marker is never touched, so RUNNING intent survives.
  "$LAUNCHCTL" bootout "$RUNTIME_SERVICE" >/dev/null 2>&1 \
    || "$LAUNCHCTL" print "$RUNTIME_SERVICE" >/dev/null 2>&1 \
    || fail "could not refresh the existing Runtime LaunchAgent."
fi
if ! "$LAUNCHCTL" print "$RUNTIME_SERVICE" >/dev/null 2>&1; then
  "$LAUNCHCTL" bootstrap "gui/$(id -u)" "$RUNTIME_PLIST" >/dev/null 2>&1 \
    || "$LAUNCHCTL" print "$RUNTIME_SERVICE" >/dev/null 2>&1 \
    || fail "could not register protected Runtime LaunchAgent."
fi
if [[ "$DESIRED_STATE_WAS_PRESENT" == "1" ]]; then
  # Re-registering a PathState job can leave an existing RUNNING intent loaded
  # but idle. Refresh the package-owned job without changing that intent.
  # launchd can report the service loaded before its new registration is
  # kickstartable, so allow a bounded registration/start retry sequence.
  RUNTIME_RESUME_OK=0
  for _attempt in 1 2 3; do
    if ! "$LAUNCHCTL" print "$RUNTIME_SERVICE" >/dev/null 2>&1; then
      "$LAUNCHCTL" bootstrap "gui/$(id -u)" "$RUNTIME_PLIST" >/dev/null 2>&1 || true
    fi
    if "$LAUNCHCTL" kickstart -k "$RUNTIME_SERVICE" >/dev/null 2>&1; then
      RUNTIME_RESUME_OK=1
      break
    fi
    sleep 1
  done
  [[ "$RUNTIME_RESUME_OK" == "1" ]] \
    || fail "could not resume the protected Runtime after installation."
fi

echo "[7/8] Verifying installed execution ownership..."
/usr/bin/python3 - "$RUNTIME_PLIST" "$TARGET_APP" "$ROOT" <<'PY'
import plistlib
import sys
from pathlib import Path

plist_path, app_path, checkout_root = map(Path, sys.argv[1:])
payload = plistlib.loads(plist_path.read_bytes())
args = payload.get("ProgramArguments", [])
runtime_root = app_path / "Contents/Resources/runtime"
expected = [str(runtime_root / "start.sh"), "--serve"]
if args[:2] != expected or len(args) != 3 or not args[2].startswith("/"):
    raise SystemExit("INSTALL ERROR: Runtime LaunchAgent does not point to installed payload")
if str(runtime_root) in " ".join(args[2:]) and str(checkout_root / "start.sh") in " ".join(args):
    raise SystemExit("INSTALL ERROR: Runtime LaunchAgent references checkout implementation")
if "RUNTIME_ENV_FILE" in payload.get("EnvironmentVariables", {}):
    raise SystemExit("INSTALL ERROR: Runtime LaunchAgent must derive canonical configuration")
PY

echo "[8/8] Installation ready."
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
