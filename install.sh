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
TUNNEL_CLIENT="$(command -v tunnel-client)"

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

PROFILE_NAME="agent-runtime"
PROFILE_DIR="$HOME/.config/tunnel-client"
PROFILE_FILE="$PROFILE_DIR/$PROFILE_NAME.yaml"
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
unset CONTROL_PLANE_API_KEY CONTROL_PLANE_TUNNEL_ID AGENT_RUNTIME_TUNNEL_PROFILE AGENT_RUNTIME_WORKSPACE_ROOT
set -a
# shellcheck disable=SC1091
source "$ENV_FILE"
set +a

LEGACY_TUNNEL_ID="${CONTROL_PLANE_TUNNEL_ID:-}"
if [[ -z "${CONTROL_PLANE_API_KEY:-}" && -n "$INHERITED_API_KEY" ]]; then
  CONTROL_PLANE_API_KEY="$INHERITED_API_KEY"
fi
[[ -n "${CONTROL_PLANE_API_KEY:-}" ]] || fail "CONTROL_PLANE_API_KEY is required in .env or the explicit install environment."

profile_tunnel_id() {
  python3 - "$1" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
lines = path.read_text().splitlines()
control_indent = None
values = []
for line in lines:
    body = line.split("#", 1)[0].rstrip()
    if not body.strip():
        continue
    indent = len(body) - len(body.lstrip())
    stripped = body.strip()
    if control_indent is None:
        if stripped == "control_plane:":
            control_indent = indent
        continue
    if indent <= control_indent:
        break
    if stripped.startswith("tunnel_id:"):
        value = stripped.split(":", 1)[1].strip().strip("\"'")
        if value:
            values.append(value)
if len(values) != 1:
    raise SystemExit(2)
print(values[0])
PY
}

if [[ -L "$PROFILE_FILE" || ( -e "$PROFILE_FILE" && ! -f "$PROFILE_FILE" ) ]]; then
  fail "canonical agent-runtime tunnel profile must be a regular non-symlink file."
fi

PROFILE_TUNNEL_ID=""
if [[ -f "$PROFILE_FILE" ]]; then
  PROFILE_TUNNEL_ID="$(profile_tunnel_id "$PROFILE_FILE")" \
    || fail "canonical agent-runtime tunnel profile is malformed or has no unique control_plane.tunnel_id."
  if [[ -n "$LEGACY_TUNNEL_ID" && "$LEGACY_TUNNEL_ID" != "$PROFILE_TUNNEL_ID" ]]; then
    fail "legacy .env tunnel identity differs from the canonical profile; migration is required."
  fi
else
  if [[ -n "$LEGACY_TUNNEL_ID" && -n "$INHERITED_TUNNEL_ID" && "$LEGACY_TUNNEL_ID" != "$INHERITED_TUNNEL_ID" ]]; then
    fail "ambiguous bootstrap tunnel identity; legacy and operator-supplied identities differ."
  fi
  BOOTSTRAP_TUNNEL_ID="${LEGACY_TUNNEL_ID:-$INHERITED_TUNNEL_ID}"
  [[ -n "$BOOTSTRAP_TUNNEL_ID" ]] || fail "canonical tunnel profile is missing and no unambiguous bootstrap tunnel identity was supplied."

  echo "[3/7] Creating canonical agent-runtime tunnel profile..."
  mkdir -p "$PROFILE_DIR"
  chmod 700 "$PROFILE_DIR"
  /usr/bin/env -i \
    "PATH=$PATH" \
    "HOME=$HOME" \
    "$TUNNEL_CLIENT" init \
      --sample sample_mcp_stdio_local \
      --profile "$PROFILE_NAME" \
      --profile-dir "$PROFILE_DIR" \
      --tunnel-id "$BOOTSTRAP_TUNNEL_ID" \
      --mcp-command "$ROOT/.venv/bin/python -m agent_runtime.server" \
      >/dev/null 2>&1 \
    || fail "tunnel-client init failed; no background service was started."
  [[ -f "$PROFILE_FILE" && ! -L "$PROFILE_FILE" ]] || fail "canonical tunnel profile was not created as a regular file."
  PROFILE_TUNNEL_ID="$(profile_tunnel_id "$PROFILE_FILE")" \
    || fail "new canonical tunnel profile is malformed."
  [[ "$PROFILE_TUNNEL_ID" == "$BOOTSTRAP_TUNNEL_ID" ]] \
    || fail "new canonical tunnel profile did not preserve the requested identity."
fi

TUNNEL_ENV=(
  /usr/bin/env -i
  "PATH=$PATH"
  "HOME=$HOME"
  "CONTROL_PLANE_API_KEY=$CONTROL_PLANE_API_KEY"
  "AGENT_RUNTIME_WORKSPACE_ROOT=$WORKSPACE_ROOT"
)
[[ -n "${USER:-}" ]] && TUNNEL_ENV+=("USER=$USER")
[[ -n "${TMPDIR:-}" ]] && TUNNEL_ENV+=("TMPDIR=$TMPDIR")
[[ -n "${LANG:-}" ]] && TUNNEL_ENV+=("LANG=$LANG")
for key in LC_ALL LC_CTYPE LC_MESSAGES; do
  [[ -n "${!key:-}" ]] && TUNNEL_ENV+=("$key=${!key}")
done

echo "[4/7] Checking canonical tunnel profile..."
"${TUNNEL_ENV[@]}" "$TUNNEL_CLIENT" doctor --profile-file "$PROFILE_FILE" --health.listen-addr 127.0.0.1:0 --explain >/dev/null 2>&1 \
  || fail "tunnel-client doctor failed; no tunnel was started."

CONTROL_PLANE_API_KEY="$CONTROL_PLANE_API_KEY" \
AGENT_RUNTIME_WORKSPACE_ROOT="$WORKSPACE_ROOT" \
python3 - "$ENV_FILE" <<'PY'
from __future__ import annotations

import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
owned = {
    "CONTROL_PLANE_API_KEY": os.environ["CONTROL_PLANE_API_KEY"],
    "AGENT_RUNTIME_WORKSPACE_ROOT": os.environ["AGENT_RUNTIME_WORKSPACE_ROOT"],
}
remove = {"CONTROL_PLANE_TUNNEL_ID", "AGENT_RUNTIME_TUNNEL_PROFILE"}
seen = {key: False for key in owned}
out: list[str] = []
for line in path.read_text().splitlines():
    key = line.split("=", 1)[0] if "=" in line else ""
    if key in remove:
        continue
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
/usr/bin/plutil -lint "$LOGIN_PLIST" >/dev/null || fail "login launch configuration is invalid."

# Deliberately do not bootstrap the LaunchAgent here. The installer must not
# start the UI or Runtime as a side effect; the UI will start on the next login.
echo "[7/7] Installation ready."
echo "Workspace root: $WORKSPACE_ROOT"
echo "Tunnel profile: $PROFILE_NAME ($PROFILE_FILE)"
echo "Native app: $TARGET_APP"
echo "Login behavior: UI only; Runtime never auto-starts."
echo "Open Agent Runtime.app and press Start, or use ./start.sh as the CLI fallback."
