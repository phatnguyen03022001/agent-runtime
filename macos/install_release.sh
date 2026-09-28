#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

fail() {
  echo "PREBUILT INSTALL ERROR: $*" >&2
  exit 2
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
RUNTIME_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
APP="$(cd "$RUNTIME_ROOT/../../.." && pwd -P)"
BUNDLE_ROOT="$(dirname "$APP")"
HANDOFF="$BUNDLE_ROOT/Agent Runtime.candidate.json"
PYTHON="$RUNTIME_ROOT/.venv/bin/python"
PREFLIGHT="$SCRIPT_DIR/install_preflight.py"
CONFIG_HELPER="$SCRIPT_DIR/runtime_config.py"
PROVENANCE="$SCRIPT_DIR/package_provenance.py"
CUTOVER="$SCRIPT_DIR/candidate_cutover.py"
CANONICAL_ENV="$HOME/Library/Application Support/Agent Runtime/runtime.env"

[[ "$APP" == */"Agent Runtime.app" && -d "$APP" && ! -L "$APP" ]] \
  || fail "release bundle app is missing or unsafe."
[[ -x "$PYTHON" && ! -L "$PYTHON" ]] \
  || fail "release bundle embedded Runtime Python is missing or unsafe."
for helper in "$PREFLIGHT" "$CONFIG_HELPER" "$PROVENANCE" "$CUTOVER"; do
  [[ -f "$helper" && ! -L "$helper" ]] || fail "release bundle Runtime helper is missing or unsafe."
done

run_cutover() {
  exec "$PYTHON" "$CUTOVER" "$@"
}

show_help() {
  cat <<'EOF'
Usage:
  install_release.sh --workspace-root <absolute-existing-directory>
  install_release.sh --activate-payload <payload-release-directory>
  install_release.sh --resume-cutover
  install_release.sh --commit-cutover
  install_release.sh --rollback-cutover
  install_release.sh --recover-partial-cutover

Fresh installation requires provisioned CONTROL_PLANE_API_KEY,
CONTROL_PLANE_TUNNEL_ID, AGENT_RUNTIME_GIT_NAME, and AGENT_RUNTIME_GIT_EMAIL.
Successful installation remains pending explicit commit.
EOF
}

case "${1-}" in
  --help|-h)
    [[ "$#" == "1" ]] || fail "usage: install_release.sh --help"
    show_help
    ;;
  --workspace-root)
    [[ "$#" == "2" ]] || fail "usage: install_release.sh --workspace-root <absolute-existing-directory>"
    [[ -f "$HANDOFF" && ! -L "$HANDOFF" ]] \
      || fail "release bundle candidate handoff is missing or unsafe."
    PAYLOAD_RELEASE="$("$PYTHON" - "$HANDOFF" "$BUNDLE_ROOT" <<'PY'
import json
import re
import sys
from pathlib import Path

handoff = Path(sys.argv[1])
bundle_root = Path(sys.argv[2])
try:
    value = json.loads(handoff.read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError):
    raise SystemExit(2)
closure = value.get("initial_payload_closure")
if not isinstance(closure, str) or re.fullmatch(r"[0-9a-f]{64}", closure) is None:
    raise SystemExit(2)
print(bundle_root / "payloads" / closure)
PY
)" || fail "release bundle handoff does not identify a valid external payload."
    WORKSPACE_ROOT="$2"
    [[ "$WORKSPACE_ROOT" == /* && -d "$WORKSPACE_ROOT" ]] \
      || fail "workspace root must be an absolute existing directory."
    "$PYTHON" "$PREFLIGHT" --prebuilt --bundle-root "$BUNDLE_ROOT" --workspace-root "$WORKSPACE_ROOT" \
      || fail "prebuilt installation preflight failed."
    "$PYTHON" "$CONFIG_HELPER" --prebuilt "$CANONICAL_ENV" "$WORKSPACE_ROOT" \
      || fail "canonical Runtime configuration initialization failed."
    unset CONTROL_PLANE_API_KEY CONTROL_PLANE_TUNNEL_ID AGENT_RUNTIME_GIT_NAME AGENT_RUNTIME_GIT_EMAIL
    "$PYTHON" "$PROVENANCE" validate-zero-cost "$APP" "$HANDOFF" "$PAYLOAD_RELEASE" >/dev/null \
      || fail "zero-cost release candidate validation failed before cutover."
    command -v launchctl >/dev/null 2>&1 || fail "launchctl is required for candidate cutover."
    "$PYTHON" "$CUTOVER" cutover "$APP" "$HANDOFF" --payload-release "$PAYLOAD_RELEASE" \
      --home "$HOME" --launchctl "$(command -v launchctl)"
    echo "Prebuilt candidate is installed and pending explicit commit."
    ;;
  --activate-payload)
    [[ "$#" == "2" ]] || fail "usage: install_release.sh --activate-payload <payload-release-directory>"
    [[ -d "$2" && ! -L "$2" ]] || fail "payload release directory is missing or unsafe."
    command -v launchctl >/dev/null 2>&1 || fail "launchctl is required for payload activation."
    run_cutover activate-payload "$2" --home "$HOME" --launchctl "$(command -v launchctl)"
    ;;
  --commit-cutover)
    [[ "$#" == "1" ]] || fail "usage: install_release.sh --commit-cutover"
    run_cutover commit --home "$HOME"
    ;;
  --resume-cutover)
    [[ "$#" == "1" ]] || fail "usage: install_release.sh --resume-cutover"
    command -v launchctl >/dev/null 2>&1 || fail "launchctl is required for candidate cutover resume."
    run_cutover resume --home "$HOME" --launchctl "$(command -v launchctl)"
    ;;
  --rollback-cutover)
    [[ "$#" == "1" ]] || fail "usage: install_release.sh --rollback-cutover"
    command -v launchctl >/dev/null 2>&1 || fail "launchctl is required for candidate rollback."
    run_cutover rollback --home "$HOME" --launchctl "$(command -v launchctl)"
    ;;
  --recover-partial-cutover)
    [[ "$#" == "1" ]] || fail "usage: install_release.sh --recover-partial-cutover"
    command -v launchctl >/dev/null 2>&1 || fail "launchctl is required for partial cutover recovery."
    run_cutover recover --home "$HOME" --launchctl "$(command -v launchctl)"
    ;;
  *)
    show_help >&2
    exit 2
    ;;
esac
