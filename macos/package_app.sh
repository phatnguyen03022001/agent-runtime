#!/usr/bin/env bash
set -euo pipefail

PACKAGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$PACKAGE_ROOT/.." && pwd -P)"
BUILD_ROOT="$REPO_ROOT/build"
CANDIDATES_ROOT="$REPO_ROOT/build/candidates"
source "$PACKAGE_ROOT/packaging_python.sh"
PYTHON_BIN="$(resolve_packaging_python "PACKAGE ERROR")"
SIGNING_IDENTITY="${AGENT_RUNTIME_CODESIGN_IDENTITY:-}"
[[ -n "$SIGNING_IDENTITY" && "$SIGNING_IDENTITY" != "-" ]] \
  || { echo "PACKAGE ERROR: AGENT_RUNTIME_CODESIGN_IDENTITY must name an explicit non-ad-hoc signing identity" >&2; exit 2; }

mkdir -p "$BUILD_ROOT"
TEMP_ROOT="$(mktemp -d "$BUILD_ROOT/.agent-runtime-package.XXXXXX")"
STAGED_PUBLICATION="$TEMP_ROOT/candidate"
APP="$STAGED_PUBLICATION/Agent Runtime.app"
CANDIDATE_HANDOFF="$STAGED_PUBLICATION/Agent Runtime.candidate.json"
CONTENTS="$APP/Contents"
MACOS="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"
RUNTIME="$RESOURCES/runtime"
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
SERVICE_PLIST="$SOURCE_PACKAGE_ROOT/AppBundle/Library/LaunchAgents/com.picmao.agent-runtime-runtime-service.plist"
[[ -f "$SOURCE_ROOT/requirements.lock" ]] \
  || { echo "PACKAGE ERROR: requirements.lock is missing from exact HEAD" >&2; exit 2; }
[[ -f "$SOURCE_ROOT/agent_runtime/server.py" ]] \
  || { echo "PACKAGE ERROR: staged agent_runtime payload is incomplete" >&2; exit 2; }
[[ -f "$SOURCE_ROOT/agent_runtime/version.py" ]] \
  || { echo "PACKAGE ERROR: Runtime version SSOT is missing" >&2; exit 2; }
RUNTIME_VERSION="$("$PYTHON_BIN" "$SOURCE_ROOT/agent_runtime/version.py")" \
  || { echo "PACKAGE ERROR: Runtime version SSOT could not be read" >&2; exit 2; }
PLIST_RUNTIME_VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$SOURCE_PACKAGE_ROOT/AppBundle/Info.plist")" \
  || { echo "PACKAGE ERROR: package Runtime version projection is missing" >&2; exit 2; }
[[ "$PLIST_RUNTIME_VERSION" == "$RUNTIME_VERSION" ]] \
  || { echo "PACKAGE ERROR: CFBundleShortVersionString does not match Runtime version SSOT" >&2; exit 2; }
[[ -f "$APP_ICON" ]] \
  || { echo "PACKAGE ERROR: approved application icon is missing" >&2; exit 2; }
[[ -f "$NOTIFICATION_SOUND" ]] \
  || { echo "PACKAGE ERROR: approved notification sound is missing" >&2; exit 2; }
[[ -f "$SERVICE_PLIST" ]] \
  || { echo "PACKAGE ERROR: app-owned Runtime LaunchAgent metadata is missing" >&2; exit 2; }

SWIFT_SCRATCH="$TEMP_ROOT/swift-build"
/usr/bin/xcrun swift build --package-path "$SOURCE_PACKAGE_ROOT" --scratch-path "$SWIFT_SCRATCH" -c release >&2
BIN_DIR="$(/usr/bin/xcrun swift build --package-path "$SOURCE_PACKAGE_ROOT" --scratch-path "$SWIFT_SCRATCH" -c release --show-bin-path)"
BINARY="$BIN_DIR/AgentRuntimeMenuBar"
RUNTIME_SERVICE_BINARY="$BIN_DIR/AgentRuntimeRuntimeService"
SCREEN_CAPTURE_BINARY="$BIN_DIR/AgentRuntimeScreenCapture"
[[ -x "$BINARY" ]] || { echo "PACKAGE ERROR: missing AgentRuntimeMenuBar binary" >&2; exit 2; }
[[ -x "$RUNTIME_SERVICE_BINARY" ]] || { echo "PACKAGE ERROR: missing AgentRuntimeRuntimeService binary" >&2; exit 2; }
[[ -x "$SCREEN_CAPTURE_BINARY" ]] || { echo "PACKAGE ERROR: missing AgentRuntimeScreenCapture binary" >&2; exit 2; }

mkdir -p "$MACOS" "$RESOURCES" "$RUNTIME/agent_runtime" "$CONTENTS/Library/LaunchAgents"
cp "$SOURCE_PACKAGE_ROOT/AppBundle/Info.plist" "$CONTENTS/Info.plist"
cp "$BINARY" "$MACOS/AgentRuntimeMenuBar"
cp "$RUNTIME_SERVICE_BINARY" "$MACOS/AgentRuntimeRuntimeService"
cp "$SCREEN_CAPTURE_BINARY" "$MACOS/AgentRuntimeScreenCapture"
cp "$SERVICE_PLIST" "$CONTENTS/Library/LaunchAgents/com.picmao.agent-runtime-runtime-service.plist"
cp "$APP_ICON" "$RESOURCES/AppIcon.png"
cp "$NOTIFICATION_SOUND" "$RESOURCES/notification.mp3"
/usr/bin/strip -S "$MACOS/AgentRuntimeMenuBar" "$MACOS/AgentRuntimeRuntimeService" "$MACOS/AgentRuntimeScreenCapture"

cp "$SOURCE_ROOT/start.sh" "$RUNTIME/start.sh"
find "$SOURCE_ROOT/agent_runtime" -maxdepth 1 -type f -name '*.py' -exec cp '{}' "$RUNTIME/agent_runtime/" \;
PACKAGE_VENV="$TEMP_ROOT/runtime-venv"
"$PYTHON_BIN" -m venv --copies --without-pip "$PACKAGE_VENV"
materialize_packaging_python_runtime "$PYTHON_BIN" "$PACKAGE_VENV" "$REPO_ROOT" "PACKAGE ERROR"
"$PACKAGE_VENV/bin/python" -c 'pass' >/dev/null 2>&1 \
  || { echo "PACKAGE ERROR: copied packaging interpreter smoke execution failed" >&2; exit 2; }
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
/usr/bin/codesign --force --deep --sign "$SIGNING_IDENTITY" "$APP" >/dev/null
/usr/bin/codesign --verify --deep --strict "$APP"
"$PYTHON_BIN" "$SOURCE_PACKAGE_ROOT/package_provenance.py" validate \
  "$RUNTIME" "$RESOURCES/runtime-manifest.json" \
  "$RUNTIME_REVISION" "$RUNTIME_TREE" "$SOURCE_ROOT/requirements.lock"
"$PYTHON_BIN" "$SOURCE_PACKAGE_ROOT/package_provenance.py" seal "$APP" "$CANDIDATE_HANDOFF" >/dev/null
PUBLISHED="$("$PYTHON_BIN" "$SOURCE_PACKAGE_ROOT/package_provenance.py" publish \
  "$APP" "$CANDIDATE_HANDOFF" "$CANDIDATES_ROOT")" \
  || { echo "PACKAGE ERROR: candidate publication failed" >&2; exit 2; }
IFS=$'\t' read -r FINAL_APP FINAL_HANDOFF CANDIDATE_SHA256 <<< "$PUBLISHED"
[[ -n "$FINAL_APP" && -n "$FINAL_HANDOFF" && "$CANDIDATE_SHA256" =~ ^[0-9a-f]{64}$ ]] \
  || { echo "PACKAGE ERROR: candidate publication result is invalid" >&2; exit 2; }

printf 'candidate_app=%s\n' "$FINAL_APP"
printf 'candidate_handoff=%s\n' "$FINAL_HANDOFF"
printf 'candidate_sha256=%s\n' "$CANDIDATE_SHA256"
