#!/usr/bin/env bash
set -euo pipefail

fail() {
  echo "INSTALL ERROR: $*" >&2
  exit 2
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
  fail "agent-runtime install.sh currently supports macOS only."
fi

command -v git >/dev/null 2>&1 || fail "git is required."
command -v python3 >/dev/null 2>&1 || fail "Python 3.11+ is required."
python3 - <<'PY' || exit 2
import sys
if sys.version_info < (3, 11):
    raise SystemExit("INSTALL ERROR: Python 3.11+ is required.")
PY

[[ -d "$ROOT/.git" ]] || fail "run install.sh from a Git clone of agent-runtime."
BRANCH="$(git branch --show-current)"
case "$BRANCH" in
  dev|main) ;;
  *) fail "installer requires the checked-out branch to be dev or main." ;;
esac

[[ -z "$(git status --porcelain)" ]] || fail "primary repository must be clean before installation."
REMOTE="$(git config --get remote.origin.url || true)"
[[ -n "$REMOTE" ]] || fail "origin remote is missing."

case "$REMOTE" in
  https://github.com/phatnguyen03022001/agent-runtime.git|git@github.com:phatnguyen03022001/agent-runtime.git)
    ;;
  *)
    fail "origin must identify phatnguyen03022001/agent-runtime exactly."
    ;;
esac

GIT_TERMINAL_PROMPT=0 git fetch origin "$BRANCH" --prune >/dev/null 2>&1 \
  || fail "unable to fetch origin/$BRANCH without prompting."
LOCAL_HEAD="$(git rev-parse HEAD)"
REMOTE_HEAD="$(git rev-parse "origin/$BRANCH")"
[[ "$LOCAL_HEAD" == "$REMOTE_HEAD" ]] \
  || fail "primary repository must exactly match origin/$BRANCH before installation."

echo "[1/6] Preparing Python runtime..."
if [[ ! -e "$ROOT/.venv" ]]; then
  python3 -m venv "$ROOT/.venv"
fi
[[ -d "$ROOT/.venv" && ! -L "$ROOT/.venv" && -x "$ROOT/.venv/bin/python" ]] \
  || fail "existing .venv is not a usable installer-owned virtual environment."
"$ROOT/.venv/bin/python" -m pip install -r "$ROOT/requirements.txt"
"$ROOT/verify"

echo "[2/6] Checking tunnel-client..."
if ! command -v tunnel-client >/dev/null 2>&1; then
  command -v brew >/dev/null 2>&1 \
    || fail "tunnel-client is missing and Homebrew is required to install the official OpenAI formula."
  brew install openai/tools/tunnel-client
fi
command -v tunnel-client >/dev/null 2>&1 || fail "tunnel-client installation did not produce a usable command."
tunnel-client --version >/dev/null 2>&1 || fail "tunnel-client --version failed."
tunnel-client init --help >/dev/null 2>&1 || fail "tunnel-client lacks the required init command."
tunnel-client doctor --help >/dev/null 2>&1 || fail "tunnel-client lacks the required doctor command."

ensure_private_dir() {
  local target="$1"
  if [[ -e "$target" || -L "$target" ]]; then
    [[ -d "$target" && ! -L "$target" ]] || fail "existing local path is not a safe directory: $target"
  else
    mkdir -p "$target"
  fi
  chmod 700 "$target"
}

CONFIG_DIR="$HOME/.config/agent-runtime"
CONFIG="$CONFIG_DIR/runtime.local.toml"
STATE_DIR="$HOME/.local/state/agent-runtime"
CHECKOUT_PARENT="$HOME/.local/share/agent-runtime/checkouts"
PROJECT_ID="agent-runtime-$BRANCH"
CHECKOUT="$CHECKOUT_PARENT/$PROJECT_ID"
PROFILE_NAME="agent-runtime"
PROFILE="$HOME/.config/tunnel-client/$PROFILE_NAME.yaml"

ensure_private_dir "$CONFIG_DIR"
ensure_private_dir "$STATE_DIR"
ensure_private_dir "$CHECKOUT_PARENT"

echo "[3/6] Preparing disposable verification checkout..."
if [[ -e "$CHECKOUT" || -L "$CHECKOUT" ]]; then
  if [[ ! -d "$CHECKOUT" || -L "$CHECKOUT" ]] \
    || ! git -C "$CHECKOUT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    fail "existing disposable checkout is not recognized; preserved without modification: $CHECKOUT"
  fi

  CHECKOUT_REMOTE="$(git -C "$CHECKOUT" config --get remote.origin.url || true)"
  CHECKOUT_BRANCH="$(git -C "$CHECKOUT" branch --show-current || true)"
  if [[ "$CHECKOUT_REMOTE" != "$REMOTE" || "$CHECKOUT_BRANCH" != "$BRANCH" ]] \
    || [[ -n "$(git -C "$CHECKOUT" status --porcelain 2>/dev/null || true)" ]]; then
    fail "existing disposable checkout has incompatible identity or local changes; preserved: $CHECKOUT"
  fi

  GIT_TERMINAL_PROMPT=0 git -C "$CHECKOUT" fetch origin "$BRANCH" --prune >/dev/null 2>&1 \
    || fail "existing disposable checkout fetch failed; preserved: $CHECKOUT"
  git -C "$CHECKOUT" merge --ff-only "origin/$BRANCH" >/dev/null 2>&1 \
    || fail "existing disposable checkout cannot fast-forward safely; preserved: $CHECKOUT"
else
  GIT_TERMINAL_PROMPT=0 git clone --branch "$BRANCH" --single-branch "$REMOTE" "$CHECKOUT" >/dev/null 2>&1 \
    || fail "unable to create disposable checkout."
fi

[[ "$(git -C "$CHECKOUT" config --get remote.origin.url || true)" == "$REMOTE" ]] \
  || fail "disposable checkout remote identity mismatch."
[[ "$(git -C "$CHECKOUT" branch --show-current || true)" == "$BRANCH" ]] \
  || fail "disposable checkout branch mismatch."
[[ -z "$(git -C "$CHECKOUT" status --porcelain)" ]] \
  || fail "disposable checkout is not clean."
CHECKOUT_HEAD="$(git -C "$CHECKOUT" rev-parse HEAD)"
CHECKOUT_REMOTE_HEAD="$(git -C "$CHECKOUT" rev-parse "origin/$BRANCH")"
[[ "$CHECKOUT_HEAD" == "$CHECKOUT_REMOTE_HEAD" ]] \
  || fail "disposable checkout does not exactly match origin/$BRANCH."

EXPECTED_CONFIG="$(cat <<EOF
version = 1
state_dir = "$STATE_DIR"

[projects.$PROJECT_ID]
repository = "phatnguyen03022001/agent-runtime"
checkout = "$CHECKOUT"
remote = "origin"
expected_remote_url = "$REMOTE"
branch = "$BRANCH"
verify_argv = ["./verify"]
timeout_seconds = 3600
disposable = true
EOF
)"

if [[ -e "$CONFIG" || -L "$CONFIG" ]]; then
  [[ -f "$CONFIG" && ! -L "$CONFIG" ]] || fail "existing runtime config is not a regular file: $CONFIG"
  [[ "$(cat "$CONFIG")" == "$EXPECTED_CONFIG" ]] \
    || fail "existing runtime config differs from the expected installer-owned configuration; preserved: $CONFIG"
else
  umask 077
  printf '%s\n' "$EXPECTED_CONFIG" > "$CONFIG"
fi
chmod 600 "$CONFIG"

echo "[4/6] Preparing local environment file..."
ENV_FILE="$ROOT/.env"
if [[ -e "$ENV_FILE" || -L "$ENV_FILE" ]]; then
  [[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || fail "existing .env is not a regular file."
else
  cp "$ROOT/.env.example" "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"

AGENT_RUNTIME_CONFIG_VALUE="$CONFIG" PROFILE_NAME_VALUE="$PROFILE_NAME" python3 - "$ENV_FILE" <<'PY'
from __future__ import annotations

import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
lines = path.read_text().splitlines()
owned = {
    "AGENT_RUNTIME_CONFIG": os.environ["AGENT_RUNTIME_CONFIG_VALUE"],
    "AGENT_RUNTIME_TUNNEL_PROFILE": os.environ["PROFILE_NAME_VALUE"],
}
counts = {key: 0 for key in (*owned, "CONTROL_PLANE_API_KEY", "CONTROL_PLANE_TUNNEL_ID")}
out: list[str] = []

for line in lines:
    key = line.split("=", 1)[0] if "=" in line else ""
    if key in counts:
        counts[key] += 1
        if counts[key] > 1:
            raise SystemExit(f"INSTALL ERROR: duplicate {key} entry in .env; preserved for manual repair.")
    if key in owned:
        out.append(f"{key}={owned[key]}")
    else:
        out.append(line)

for key, value in owned.items():
    if counts[key] == 0:
        out.append(f"{key}={value}")

path.write_text("\n".join(out) + "\n")
PY

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

MISSING=()
[[ -n "${CONTROL_PLANE_API_KEY:-}" ]] || MISSING+=("CONTROL_PLANE_API_KEY")
[[ -n "${CONTROL_PLANE_TUNNEL_ID:-}" ]] || MISSING+=("CONTROL_PLANE_TUNNEL_ID")
if (( ${#MISSING[@]} > 0 )); then
  echo "INSTALL ERROR: missing required credentials: ${MISSING[*]}" >&2
  echo "Set them in .env or export them, then rerun ./install.sh. Literal credential values are never printed." >&2
  exit 2
fi

if [[ "${AGENT_RUNTIME_CONFIG:-}" != "$CONFIG" || "${AGENT_RUNTIME_TUNNEL_PROFILE:-}" != "$PROFILE_NAME" ]]; then
  fail "installer-owned .env values did not load as expected."
fi

echo "[5/6] Checking tunnel profile..."
if [[ -e "$PROFILE" || -L "$PROFILE" ]]; then
  [[ -f "$PROFILE" && ! -L "$PROFILE" ]] \
    || fail "existing tunnel profile is not a regular file; preserved: $PROFILE"
else
  tunnel-client init \
    --sample sample_mcp_stdio_local \
    --profile "$PROFILE_NAME" \
    --tunnel-id "$CONTROL_PLANE_TUNNEL_ID" \
    --mcp-command "$ROOT/.venv/bin/python -m agent_runtime.server" \
    >/dev/null 2>&1 \
    || fail "tunnel-client init failed; no daemon was started."
  [[ -f "$PROFILE" && ! -L "$PROFILE" ]] \
    || fail "tunnel-client init did not create the expected profile."
fi

if ! tunnel-client doctor --profile "$PROFILE_NAME" --explain >/dev/null 2>&1; then
  fail "tunnel-client doctor failed; no daemon was started. Run doctor manually for diagnostics."
fi

echo "[6/6] Installation ready."
echo "Runtime config: $CONFIG"
echo "Disposable project: $PROJECT_ID"
echo "Tunnel profile: $PROFILE_NAME"
echo "Next step: ./start.sh"
