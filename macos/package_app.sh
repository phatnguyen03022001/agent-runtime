#!/usr/bin/env bash
set -euo pipefail

PACKAGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$PACKAGE_ROOT/.." && pwd -P)"
APP="$REPO_ROOT/build/Agent Runtime.app"
CANDIDATE_HANDOFF="$REPO_ROOT/build/Agent Runtime.candidate.json"
CONTENTS="$APP/Contents"
MACOS="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"
RUNTIME="$RESOURCES/runtime"
source "$PACKAGE_ROOT/packaging_python.sh"
PYTHON_BIN="$(resolve_packaging_python "PACKAGE ERROR")"

TEMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/agent-runtime-package.XXXXXX")"
cleanup() {
  chmod -R u+w "$TEMP_ROOT" 2>/dev/null || true
  rm -rf "$TEMP_ROOT"
}
trap cleanup EXIT
SOURCE_ROOT="$TEMP_ROOT/source"
mkdir -p "$SOURCE_ROOT"
IDENTITY="$("$PYTHON_BIN" "$PACKAGE_ROOT/package_provenance.py" stage "$REPO_ROOT" "$SOURCE_ROOT")" \
  || { echo "PACKAGE ERROR: immutable source staging failed" >&2; exit 2; }
IFS=$'\t' read -r RUNTIME_REVISION RUNTIME_TREE <<< "$IDENTITY"
[[ "$RUNTIME_REVISION" =~ ^[0-9a-f]{40}$ && "$RUNTIME_TREE" =~ ^[0-9a-f]{40}$ ]] \
  || { echo "PACKAGE ERROR: staged Git identity is invalid" >&2; exit 2; }
chmod -R a-w "$SOURCE_ROOT"
SOURCE_PACKAGE_ROOT="$SOURCE_ROOT/macos"
APP_ICON="$SOURCE_PACKAGE_ROOT/AppBundle/Resources/AppIcon.png"
NOTIFICATION_SOUND="$SOURCE_PACKAGE_ROOT/AppBundle/Resources/notification.mp3"
[[ -f "$SOURCE_ROOT/requirements.lock" ]] \
  || { echo "PACKAGE ERROR: requirements.lock is missing from exact HEAD" >&2; exit 2; }
[[ -f "$SOURCE_ROOT/agent_runtime/server.py" ]] \
  || { echo "PACKAGE ERROR: staged agent_runtime payload is incomplete" >&2; exit 2; }
[[ -f "$APP_ICON" ]] \
  || { echo "PACKAGE ERROR: approved application icon is missing" >&2; exit 2; }
[[ -f "$NOTIFICATION_SOUND" ]] \
  || { echo "PACKAGE ERROR: approved notification sound is missing" >&2; exit 2; }

SWIFT_SCRATCH="$TEMP_ROOT/swift-build"
/usr/bin/xcrun swift build --package-path "$SOURCE_PACKAGE_ROOT" --scratch-path "$SWIFT_SCRATCH" -c release
BIN_DIR="$(/usr/bin/xcrun swift build --package-path "$SOURCE_PACKAGE_ROOT" --scratch-path "$SWIFT_SCRATCH" -c release --show-bin-path)"
BINARY="$BIN_DIR/AgentRuntimeMenuBar"
[[ -x "$BINARY" ]] || { echo "PACKAGE ERROR: missing AgentRuntimeMenuBar binary" >&2; exit 2; }

rm -rf "$APP"
rm -f "$CANDIDATE_HANDOFF"
mkdir -p "$MACOS" "$RESOURCES" "$RUNTIME/agent_runtime"
cp "$SOURCE_PACKAGE_ROOT/AppBundle/Info.plist" "$CONTENTS/Info.plist"
cp "$BINARY" "$MACOS/AgentRuntimeMenuBar"
cp "$APP_ICON" "$RESOURCES/AppIcon.png"
cp "$NOTIFICATION_SOUND" "$RESOURCES/notification.mp3"
/usr/bin/strip -S "$MACOS/AgentRuntimeMenuBar"

cp "$SOURCE_ROOT/start.sh" "$RUNTIME/start.sh"
find "$SOURCE_ROOT/agent_runtime" -maxdepth 1 -type f -name '*.py' -exec cp '{}' "$RUNTIME/agent_runtime/" \;
PACKAGE_VENV="$TEMP_ROOT/runtime-venv"
"$PYTHON_BIN" -m venv --copies --without-pip "$PACKAGE_VENV"
"$PYTHON_BIN" -m pip --disable-pip-version-check --python "$PACKAGE_VENV/bin/python" \
  install --require-hashes -r "$SOURCE_ROOT/requirements.lock" >/dev/null
find "$PACKAGE_VENV" -type d -name '__pycache__' -prune -exec rm -rf '{}' +
find "$PACKAGE_VENV" -type f -name '*.pyc' -delete
find "$PACKAGE_VENV/bin" -type f ! -name 'python' -delete
find "$PACKAGE_VENV" -type l -delete
TMP_PYVENV="$PACKAGE_VENV/.pyvenv.cfg.$$"
grep -E '^(home|include-system-site-packages|version|executable) = ' \
  "$PACKAGE_VENV/pyvenv.cfg" > "$TMP_PYVENV"
mv -f "$TMP_PYVENV" "$PACKAGE_VENV/pyvenv.cfg"
/bin/cp -R "$PACKAGE_VENV" "$RUNTIME/.venv"
chmod 755 "$RUNTIME/start.sh" "$RUNTIME/.venv/bin/python"

"$PYTHON_BIN" "$SOURCE_PACKAGE_ROOT/package_provenance.py" manifest \
  "$RUNTIME" "$RESOURCES/runtime-manifest.json" \
  "$RUNTIME_REVISION" "$RUNTIME_TREE" "$SOURCE_ROOT/requirements.lock" \
  || { echo "PACKAGE ERROR: runtime manifest generation failed" >&2; exit 2; }

/usr/bin/plutil -lint "$CONTENTS/Info.plist" >/dev/null
[[ "$(/usr/libexec/PlistBuddy -c 'Print :LSUIElement' "$CONTENTS/Info.plist")" == "true" ]] \
  || { echo "PACKAGE ERROR: LSUIElement must be true" >&2; exit 2; }
/usr/bin/codesign --force --deep --sign - "$APP" >/dev/null
/usr/bin/codesign --verify --deep --strict "$APP"
"$PYTHON_BIN" "$SOURCE_PACKAGE_ROOT/package_provenance.py" validate \
  "$RUNTIME" "$RESOURCES/runtime-manifest.json" \
  "$RUNTIME_REVISION" "$RUNTIME_TREE" "$SOURCE_ROOT/requirements.lock"
"$PYTHON_BIN" "$SOURCE_PACKAGE_ROOT/package_provenance.py" seal "$APP" "$CANDIDATE_HANDOFF" >/dev/null

echo "$APP"
