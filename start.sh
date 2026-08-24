#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [[ ! -f "$ROOT/.env" ]]; then
  echo "Missing $ROOT/.env; copy .env.example and set local values." >&2
  exit 2
fi

set -a
# shellcheck disable=SC1091
source "$ROOT/.env"
set +a

PROFILE="${AGENT_RUNTIME_TUNNEL_PROFILE:-agent-runtime}"
: "${AGENT_RUNTIME_CONFIG:?AGENT_RUNTIME_CONFIG must point to the trusted local TOML config}"

if [[ "$AGENT_RUNTIME_CONFIG" != /* ]]; then
  echo "AGENT_RUNTIME_CONFIG must be an absolute path." >&2
  exit 2
fi

echo "[1/2] Checking agent-runtime tunnel profile..."
tunnel-client doctor --profile "$PROFILE" --explain

echo
echo "[2/2] Starting agent-runtime tunnel..."
exec tunnel-client run --profile "$PROFILE"
