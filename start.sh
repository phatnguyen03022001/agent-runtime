#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$ROOT"

if [[ ! -f "$ROOT/.env" ]]; then
  echo "Missing $ROOT/.env; run ./install.sh first." >&2
  exit 2
fi

set -a
# shellcheck disable=SC1091
source "$ROOT/.env"
set +a

PROFILE="${AGENT_RUNTIME_TUNNEL_PROFILE:-agent-runtime}"
: "${AGENT_RUNTIME_WORKSPACE_ROOT:?AGENT_RUNTIME_WORKSPACE_ROOT must be configured}"

if [[ "$AGENT_RUNTIME_WORKSPACE_ROOT" != /* || ! -d "$AGENT_RUNTIME_WORKSPACE_ROOT" ]]; then
  echo "AGENT_RUNTIME_WORKSPACE_ROOT must be an absolute existing directory." >&2
  exit 2
fi

command -v tunnel-client >/dev/null 2>&1 || {
  echo "tunnel-client is required; install it before starting agent-runtime." >&2
  exit 2
}

echo "[1/2] Checking agent-runtime tunnel profile..."
tunnel-client doctor --profile "$PROFILE" --explain

echo
echo "[2/2] Starting agent-runtime in this foreground Terminal..."
exec tunnel-client run --profile "$PROFILE"
