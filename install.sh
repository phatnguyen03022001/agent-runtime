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
LEGACY_CONFIG="$HOME/.config/tunnel-client/agent-runtime.yaml"
RUNTIME_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
[[ ! -e "$LEGACY_CONFIG" && ! -L "$LEGACY_CONFIG" ]] \
  || fail "Legacy tunnel configuration must remain absent at $LEGACY_CONFIG."
[[ "$TUNNEL_CLIENT" == /* && -f "$TUNNEL_CLIENT" && -x "$TUNNEL_CLIENT" ]] \
  || fail "resolved tunnel-client must be an absolute executable."

if [[ ! -e "$ROOT/.venv" ]]; then
  echo "[1/7] Creating local Python environment..."
  python3 -m venv "$ROOT/.venv"
fi
[[ -d "$ROOT/.venv" && ! -L "$ROOT/.venv" && -x "$ROOT/.venv/bin/python" ]] \
  || fail "existing .venv is not a usable local virtual environment."

"$ROOT/.venv/bin/python" -m pip install -r "$ROOT/requirements.txt"
PYTHON="$ROOT/.venv/bin/python" "$ROOT/verify"

if [[ -e "$ENV_FILE" || -L "$ENV_FILE" ]]; then
  [[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || fail "existing .env is not a regular file."
else
  echo "[2/7] Creating ignored local environment file..."
  cp "$ROOT/.env.example" "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"

echo "[3/8] Validating checkout-local tunnel authority..."
INSTALL_API_KEY="${CONTROL_PLANE_API_KEY-}" \
INSTALL_TUNNEL_ID="${CONTROL_PLANE_TUNNEL_ID-}" \
/usr/bin/python3 - "$ENV_FILE" "$TUNNEL_CLIENT" "$ROOT/.venv/bin/python" "$WORKSPACE_ROOT" "$HOME" "$RUNTIME_PATH" <<'PY'
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

def fail(message):
    print("INSTALL ERROR: " + message, file=sys.stderr)
    raise SystemExit(2)

try:
    lines = env_file.read_text(encoding="utf-8").splitlines()
except OSError as exc:
    fail("Could not read checkout-local .env: " + str(exc))

required = ("CONTROL_PLANE_API_KEY", "CONTROL_PLANE_TUNNEL_ID", "AGENT_RUNTIME_WORKSPACE_ROOT")
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
    if key in required:
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
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    check=False,
)
if check.returncode != 0:
    fail("tunnel-client configuration check failed; Runtime was not started.")

payload = [
    "CONTROL_PLANE_API_KEY=" + values["CONTROL_PLANE_API_KEY"],
    "CONTROL_PLANE_TUNNEL_ID=" + values["CONTROL_PLANE_TUNNEL_ID"],
    "AGENT_RUNTIME_WORKSPACE_ROOT=" + values["AGENT_RUNTIME_WORKSPACE_ROOT"],
]
payload.extend(other)
temporary = env_file.with_name("." + env_file.name + ".tmp")
temporary.write_text("\n".join(payload).rstrip("\n") + "\n", encoding="utf-8")
temporary.chmod(0o600)
temporary.replace(env_file)
PY
chmod 600 "$ENV_FILE"

echo "[5/7] Building native menu-bar app..."
"$ROOT/macos/package_app.sh" >/dev/null
SOURCE_APP="$ROOT/build/Agent Runtime.app"
TARGET_APPS="$HOME/Applications"
TARGET_APP="$TARGET_APPS/Agent Runtime.app"
mkdir -p "$TARGET_APPS"
if [[ -L "$TARGET_APP" ]]; then
  fail "existing $TARGET_APP must not be a symlink."
fi
if [[ -e "$TARGET_APP" ]]; then
  EXISTING_ID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$TARGET_APP/Contents/Info.plist" 2>/dev/null || true)"
  [[ "$EXISTING_ID" == "com.picmao.agent-runtime" ]] || fail "existing $TARGET_APP is not owned by agent-runtime."
  rm -rf "$TARGET_APP"
fi
/usr/bin/ditto "$SOURCE_APP" "$TARGET_APP"
/usr/bin/codesign --verify --deep --strict "$TARGET_APP" || fail "installed Agent Runtime.app failed code-signature verification."

echo "[6/8] Installing UI-only login launch configuration..."
LOGIN_DIR="$HOME/Library/LaunchAgents"
LOGIN_PLIST="$LOGIN_DIR/com.picmao.agent-runtime-ui.plist"
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
    <string>com.picmao.agent-runtime-ui</string>
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

# Deliberately do not bootstrap the LaunchAgent here. The installer must not
# start the UI or Runtime as a side effect; the UI will start on the next login.
echo "[7/8] Installing protected Runtime LaunchAgent..."
RUNTIME_LABEL="com.picmao.agent-runtime-runtime"
RUNTIME_PLIST="$LOGIN_DIR/$RUNTIME_LABEL.plist"
STATE_DIR="$HOME/Library/Application Support/Agent Runtime"
DESIRED_STATE="$STATE_DIR/protected-runtime-running"
mkdir -p "$STATE_DIR"
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
        <string>$ROOT/start.sh</string>
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
if ! "$LAUNCHCTL" print "$RUNTIME_SERVICE" >/dev/null 2>&1; then
  "$LAUNCHCTL" bootstrap "gui/$(id -u)" "$RUNTIME_PLIST" >/dev/null 2>&1     || "$LAUNCHCTL" print "$RUNTIME_SERVICE" >/dev/null 2>&1     || fail "could not register protected Runtime LaunchAgent."
fi

echo "[8/8] Installation ready."
echo "Workspace root: $WORKSPACE_ROOT"
echo "Tunnel authority: checkout-local .env"
echo "Native app: $TARGET_APP"
echo "Login behavior: UI launches; Runtime follows explicit persisted desired state."
echo "Open Agent Runtime.app and press Start, or use ./start.sh start as the operator CLI fallback."
