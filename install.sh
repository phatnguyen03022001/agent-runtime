#!/usr/bin/env bash
set -euo pipefail

fail() {
  echo "INSTALL ERROR: $*" >&2
  exit 2
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$ROOT"

[[ "$(uname -s)" == "Darwin" ]] || fail "agent-runtime install.sh supports macOS only."
command -v git >/dev/null 2>&1 || fail "git is required."
command -v python3 >/dev/null 2>&1 || fail "Python 3.11+ is required."
command -v tunnel-client >/dev/null 2>&1 || fail "tunnel-client is required; install the official OpenAI tunnel-client first."
command -v xcrun >/dev/null 2>&1 || fail "Xcode command-line tools are required for the native menu-bar app."
xcrun --find swift >/dev/null 2>&1 || fail "Swift is required for the native menu-bar app."

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
  https://github.com/phatnguyen03022001/agent-runtime.git|git@github.com:phatnguyen03022001/agent-runtime.git)
    ;;
  *) fail "origin must identify phatnguyen03022001/agent-runtime exactly." ;;
esac

WORKSPACE_ROOT="$(dirname "$ROOT")"
[[ "$WORKSPACE_ROOT" == /* && -d "$WORKSPACE_ROOT" ]] || fail "derived workspace root must be an absolute existing directory."

PROFILE_NAME="agent-runtime"
PROFILE="$HOME/.config/tunnel-client/$PROFILE_NAME.yaml"
ENV_FILE="$ROOT/.env"

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

INHERITED_API_KEY="${CONTROL_PLANE_API_KEY-}"
INHERITED_TUNNEL_ID="${CONTROL_PLANE_TUNNEL_ID-}"
set -a
# shellcheck disable=SC1091
source "$ENV_FILE"
set +a

if [[ -z "${CONTROL_PLANE_API_KEY:-}" && -n "$INHERITED_API_KEY" ]]; then
  CONTROL_PLANE_API_KEY="$INHERITED_API_KEY"
  export CONTROL_PLANE_API_KEY
fi
if [[ -z "${CONTROL_PLANE_TUNNEL_ID:-}" && -n "$INHERITED_TUNNEL_ID" ]]; then
  CONTROL_PLANE_TUNNEL_ID="$INHERITED_TUNNEL_ID"
  export CONTROL_PLANE_TUNNEL_ID
fi

[[ -n "${CONTROL_PLANE_API_KEY:-}" ]] || fail "CONTROL_PLANE_API_KEY is required in .env or the operator environment."
[[ -n "${CONTROL_PLANE_TUNNEL_ID:-}" ]] || fail "CONTROL_PLANE_TUNNEL_ID is required in .env or the operator environment."

CONTROL_PLANE_API_KEY="$CONTROL_PLANE_API_KEY" \
CONTROL_PLANE_TUNNEL_ID="$CONTROL_PLANE_TUNNEL_ID" \
AGENT_RUNTIME_WORKSPACE_ROOT="$WORKSPACE_ROOT" \
AGENT_RUNTIME_TUNNEL_PROFILE="$PROFILE_NAME" \
python3 - "$ENV_FILE" <<'PY'
from __future__ import annotations

import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
owned = {
    "CONTROL_PLANE_API_KEY": os.environ["CONTROL_PLANE_API_KEY"],
    "CONTROL_PLANE_TUNNEL_ID": os.environ["CONTROL_PLANE_TUNNEL_ID"],
    "AGENT_RUNTIME_WORKSPACE_ROOT": os.environ["AGENT_RUNTIME_WORKSPACE_ROOT"],
    "AGENT_RUNTIME_TUNNEL_PROFILE": os.environ["AGENT_RUNTIME_TUNNEL_PROFILE"],
}
seen = {key: False for key in owned}
out: list[str] = []
for line in path.read_text().splitlines():
    key = line.split("=", 1)[0] if "=" in line else ""
    if key in owned:
        if seen[key]:
            raise SystemExit(f"INSTALL ERROR: duplicate {key} entry in .env; repair it manually.")
        seen[key] = True
        out.append(f"{key}={owned[key]}")
    else:
        out.append(line)
for key, value in owned.items():
    if not seen[key]:
        out.append(f"{key}={value}")
path.write_text("\n".join(out) + "\n")
PY
chmod 600 "$ENV_FILE"

echo "[3/7] Preparing exact agent-runtime tunnel profile..."
mkdir -p "$HOME/.config/tunnel-client"
chmod 700 "$HOME/.config/tunnel-client"
if [[ -e "$PROFILE" || -L "$PROFILE" ]]; then
  [[ -f "$PROFILE" && ! -L "$PROFILE" ]] || fail "existing agent-runtime tunnel profile is not a regular file."
else
  tunnel-client init \
    --sample sample_mcp_stdio_local \
    --profile "$PROFILE_NAME" \
    --tunnel-id "$CONTROL_PLANE_TUNNEL_ID" \
    --mcp-command "$ROOT/.venv/bin/python -m agent_runtime.server" \
    >/dev/null 2>&1 \
    || fail "tunnel-client init failed; no background service was started."
fi

[[ -f "$PROFILE" && ! -L "$PROFILE" ]] || fail "agent-runtime tunnel profile is unavailable."

echo "[4/7] Checking tunnel profile..."
tunnel-client doctor --profile "$PROFILE_NAME" --health.listen-addr 127.0.0.1:0 --explain >/dev/null 2>&1 \
  || fail "tunnel-client doctor failed; no tunnel was started."

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
  [[ "$EXISTING_ID" == "com.picmao.agent-runtime" ]] \
    || fail "existing $TARGET_APP is not owned by agent-runtime."
  rm -rf "$TARGET_APP"
fi
/usr/bin/ditto "$SOURCE_APP" "$TARGET_APP"
/usr/bin/codesign --verify --deep --strict "$TARGET_APP" \
  || fail "installed Agent Runtime.app failed code-signature verification."

echo "[6/7] Installing UI-only login launch configuration..."
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
/usr/bin/plutil -lint "$LOGIN_PLIST" >/dev/null \
  || fail "login launch configuration is invalid."

# Deliberately do not bootstrap the LaunchAgent here. The installer must not
# start the UI or Runtime as a side effect; the UI will start on the next login.
echo "[7/7] Installation ready."
echo "Workspace root: $WORKSPACE_ROOT"
echo "Tunnel profile: $PROFILE_NAME"
echo "Native app: $TARGET_APP"
echo "Login behavior: UI only; Runtime never auto-starts."
echo "Open Agent Runtime.app and press Start, or use ./start.sh as the CLI fallback."
