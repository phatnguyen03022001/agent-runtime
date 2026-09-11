#!/usr/bin/env bash
set -euo pipefail

fail() {
  echo "START ERROR: $*" >&2
  exit 2
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$ROOT"
ENV_FILE="$ROOT/.env"
PROFILE_NAME="agent-runtime"
PROFILE_DIR="$HOME/.config/tunnel-client"
PROFILE_FILE="$PROFILE_DIR/$PROFILE_NAME.yaml"

[[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || fail "Missing or invalid $ENV_FILE; run ./install.sh first."

unset CONTROL_PLANE_API_KEY CONTROL_PLANE_TUNNEL_ID AGENT_RUNTIME_TUNNEL_PROFILE AGENT_RUNTIME_WORKSPACE_ROOT
set -a
# shellcheck disable=SC1091
source "$ENV_FILE"
set +a

[[ -n "${CONTROL_PLANE_API_KEY:-}" ]] || fail "CONTROL_PLANE_API_KEY must be configured in $ENV_FILE."
: "${AGENT_RUNTIME_WORKSPACE_ROOT:?AGENT_RUNTIME_WORKSPACE_ROOT must be configured}"
[[ "$AGENT_RUNTIME_WORKSPACE_ROOT" == /* && -d "$AGENT_RUNTIME_WORKSPACE_ROOT" ]] \
  || fail "AGENT_RUNTIME_WORKSPACE_ROOT must be an absolute existing directory."
[[ -f "$PROFILE_FILE" && ! -L "$PROFILE_FILE" ]] \
  || fail "Canonical tunnel profile must be a regular file at $PROFILE_FILE."
command -v python3 >/dev/null 2>&1 || fail "python3 is required."
command -v tunnel-client >/dev/null 2>&1 || fail "tunnel-client is required; install it before starting agent-runtime."
TUNNEL_CLIENT="$(command -v tunnel-client)"

PROFILE_TUNNEL_ID="$(python3 - "$PROFILE_FILE" <<'PY'
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
)" || fail "Canonical tunnel profile is malformed or has no unique control_plane.tunnel_id."

LEGACY_TUNNEL_ID="${CONTROL_PLANE_TUNNEL_ID:-}"
if [[ -n "$LEGACY_TUNNEL_ID" && "$LEGACY_TUNNEL_ID" != "$PROFILE_TUNNEL_ID" ]]; then
  fail "Legacy .env tunnel identity differs from the canonical profile; migration is required before restart."
fi

TUNNEL_ENV=(
  /usr/bin/env -i
  "PATH=$PATH"
  "HOME=$HOME"
  "CONTROL_PLANE_API_KEY=$CONTROL_PLANE_API_KEY"
  "AGENT_RUNTIME_WORKSPACE_ROOT=$AGENT_RUNTIME_WORKSPACE_ROOT"
)
[[ -n "${USER:-}" ]] && TUNNEL_ENV+=("USER=$USER")
[[ -n "${TMPDIR:-}" ]] && TUNNEL_ENV+=("TMPDIR=$TMPDIR")
[[ -n "${LANG:-}" ]] && TUNNEL_ENV+=("LANG=$LANG")
for key in LC_ALL LC_CTYPE LC_MESSAGES; do
  [[ -n "${!key:-}" ]] && TUNNEL_ENV+=("$key=${!key}")
done

echo "[1/2] Checking canonical agent-runtime tunnel profile..."
"${TUNNEL_ENV[@]}" "$TUNNEL_CLIENT" doctor --profile-file "$PROFILE_FILE" --explain

if [[ -n "$LEGACY_TUNNEL_ID" || -n "${AGENT_RUNTIME_TUNNEL_PROFILE:-}" ]]; then
  python3 - "$ENV_FILE" <<'PY_ENV'
from pathlib import Path
import sys

path = Path(sys.argv[1])
remove = {"CONTROL_PLANE_TUNNEL_ID", "AGENT_RUNTIME_TUNNEL_PROFILE"}
out = []
for line in path.read_text().splitlines():
    key = line.split("=", 1)[0] if "=" in line else ""
    if key not in remove:
        out.append(line)
path.write_text("\n".join(out) + "\n")
PY_ENV
  chmod 600 "$ENV_FILE"
fi

echo
echo "[2/2] Starting agent-runtime in the foreground..."
exec "${TUNNEL_ENV[@]}" "$TUNNEL_CLIENT" run --profile-file "$PROFILE_FILE"
