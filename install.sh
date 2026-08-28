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
  echo "[1/5] Creating local Python environment..."
  python3 -m venv "$ROOT/.venv"
fi
[[ -d "$ROOT/.venv" && ! -L "$ROOT/.venv" && -x "$ROOT/.venv/bin/python" ]] \
  || fail "existing .venv is not a usable local virtual environment."

"$ROOT/.venv/bin/python" -m pip install -r "$ROOT/requirements.txt"
PYTHON="$ROOT/.venv/bin/python" "$ROOT/verify"

if [[ -e "$ENV_FILE" || -L "$ENV_FILE" ]]; then
  [[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || fail "existing .env is not a regular file."
else
  echo "[2/5] Creating ignored local environment file..."
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

echo "[3/5] Preparing exact agent-runtime tunnel profile..."
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

echo "[4/5] Checking tunnel profile..."
tunnel-client doctor --profile "$PROFILE_NAME" --explain >/dev/null 2>&1 \
  || fail "tunnel-client doctor failed; no tunnel was started."

echo "[5/5] Installation ready."
echo "Workspace root: $WORKSPACE_ROOT"
echo "Tunnel profile: $PROFILE_NAME"
echo "Next step: keep ./start.sh running in a foreground Terminal when local execution is wanted."
