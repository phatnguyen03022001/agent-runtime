#!/usr/bin/env bash
set -euo pipefail

PACKAGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$PACKAGE_ROOT/.." && pwd -P)"
BUILD_ROOT="$REPO_ROOT/build"
CANDIDATES_ROOT="$BUILD_ROOT/candidates"

if [[ "$#" != "1" || "$1" != "--zero-cost" ]]; then
  echo "PACKAGE ERROR: usage: package_app.sh --zero-cost" >&2
  exit 2
fi

source "$PACKAGE_ROOT/packaging_python.sh"
PYTHON_BIN="$(resolve_packaging_python "PACKAGE ERROR")"

mkdir -p "$BUILD_ROOT"
TEMP_ROOT="$(mktemp -d "$BUILD_ROOT/.agent-runtime-package.XXXXXX")"
STAGED_PUBLICATION="$TEMP_ROOT/candidate"
APP="$STAGED_PUBLICATION/Agent Runtime.app"
CANDIDATE_HANDOFF="$STAGED_PUBLICATION/Agent Runtime.candidate.json"
PAYLOADS_ROOT="$STAGED_PUBLICATION/payloads"
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
[[ -f "$SOURCE_ROOT/requirements.lock" ]] \
  || { echo "PACKAGE ERROR: requirements.lock is missing from exact HEAD" >&2; exit 2; }
[[ -f "$SOURCE_ROOT/agent_runtime/server.py" && -f "$SOURCE_ROOT/agent_runtime/version.py" ]] \
  || { echo "PACKAGE ERROR: staged external agent_runtime payload is incomplete" >&2; exit 2; }
RUNTIME_VERSION="$("$PYTHON_BIN" "$SOURCE_ROOT/agent_runtime/version.py")" \
  || { echo "PACKAGE ERROR: Runtime version SSOT could not be read" >&2; exit 2; }
PLIST_RUNTIME_VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$SOURCE_PACKAGE_ROOT/AppBundle/Info.plist")" \
  || { echo "PACKAGE ERROR: package Runtime version projection is missing" >&2; exit 2; }
[[ "$PLIST_RUNTIME_VERSION" == "$RUNTIME_VERSION" ]] \
  || { echo "PACKAGE ERROR: CFBundleShortVersionString does not match Runtime version SSOT" >&2; exit 2; }
[[ -f "$APP_ICON" && -f "$NOTIFICATION_SOUND" ]] \
  || { echo "PACKAGE ERROR: approved app resources are incomplete" >&2; exit 2; }

SWIFT_SCRATCH="$TEMP_ROOT/swift-build"
/usr/bin/xcrun swift build --package-path "$SOURCE_PACKAGE_ROOT" --scratch-path "$SWIFT_SCRATCH" -c release >&2
BIN_DIR="$(/usr/bin/xcrun swift build --package-path "$SOURCE_PACKAGE_ROOT" --scratch-path "$SWIFT_SCRATCH" -c release --show-bin-path)"
BINARY="$BIN_DIR/AgentRuntimeMenuBar"
RUNTIME_SERVICE_BINARY="$BIN_DIR/AgentRuntimeRuntimeService"
SCREEN_CAPTURE_BINARY="$BIN_DIR/AgentRuntimeScreenCapture"
[[ -x "$BINARY" && -x "$RUNTIME_SERVICE_BINARY" && -x "$SCREEN_CAPTURE_BINARY" ]] \
  || { echo "PACKAGE ERROR: native product binaries are incomplete" >&2; exit 2; }

mkdir -p "$MACOS" "$RESOURCES" "$RUNTIME/macos" "$PAYLOADS_ROOT"
cp "$SOURCE_PACKAGE_ROOT/AppBundle/Info.plist" "$CONTENTS/Info.plist"
cp "$BINARY" "$MACOS/AgentRuntimeMenuBar"
cp "$RUNTIME_SERVICE_BINARY" "$MACOS/AgentRuntimeRuntimeService"
cp "$SCREEN_CAPTURE_BINARY" "$MACOS/AgentRuntimeScreenCapture"
cp "$APP_ICON" "$RESOURCES/AppIcon.png"
cp "$NOTIFICATION_SOUND" "$RESOURCES/notification.mp3"
/usr/bin/strip -S "$MACOS/AgentRuntimeMenuBar" "$MACOS/AgentRuntimeRuntimeService" "$MACOS/AgentRuntimeScreenCapture"

cp "$SOURCE_ROOT/start.sh" "$RUNTIME/start.sh"
cp "$SOURCE_PACKAGE_ROOT/runtime_config.py" "$RUNTIME/macos/runtime_config.py"
cp "$SOURCE_PACKAGE_ROOT/package_provenance.py" "$RUNTIME/macos/package_provenance.py"
cp "$SOURCE_PACKAGE_ROOT/candidate_cutover.py" "$RUNTIME/macos/candidate_cutover.py"
cp "$SOURCE_PACKAGE_ROOT/install_preflight.py" "$RUNTIME/macos/install_preflight.py"
cp "$SOURCE_PACKAGE_ROOT/install_release.sh" "$RUNTIME/macos/install_release.sh"
cp "$SOURCE_PACKAGE_ROOT/recover_runtime_service.py" "$RUNTIME/macos/recover_runtime_service.py"
cp "$SOURCE_PACKAGE_ROOT/uninstall.py" "$RUNTIME/macos/uninstall.py"
chmod 755 "$RUNTIME/start.sh" "$RUNTIME/macos/install_release.sh"

PACKAGE_VENV="$TEMP_ROOT/runtime-venv"
"$PYTHON_BIN" -m venv --copies --without-pip "$PACKAGE_VENV"
materialize_packaging_python_runtime "$PYTHON_BIN" "$PACKAGE_VENV" "$REPO_ROOT" "PACKAGE ERROR"
"$PYTHON_BIN" -m pip --disable-pip-version-check --python "$PACKAGE_VENV/bin/python" \
  install --require-hashes -r "$SOURCE_ROOT/requirements.lock" >/dev/null
find "$PACKAGE_VENV" -type d -name '__pycache__' -prune -exec rm -rf '{}' +
find "$PACKAGE_VENV" -type f -name '*.pyc' -delete
find "$PACKAGE_VENV/bin" -type f ! -name 'python' -delete
find "$PACKAGE_VENV" -type l -delete
finalize_packaging_python_runtime "$PYTHON_BIN" "$PACKAGE_VENV" "$REPO_ROOT" "PACKAGE ERROR"

PACKAGE_PYTHON_HOME="$TEMP_ROOT/package-python-home"
mkdir "$PACKAGE_PYTHON_HOME"
/usr/bin/env -u PYTHONHOME -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 HOME="$PACKAGE_PYTHON_HOME" \
  "$PACKAGE_VENV/bin/python" -c 'import os, platform, sys, sysconfig; prefix=os.path.realpath(sys.prefix); base=os.path.realpath(sys.base_prefix); stdlib=os.path.realpath(sysconfig.get_path("stdlib")); module=os.path.realpath(os.__file__); assert sys.version_info[:2] == (3, 13); assert platform.machine() == "arm64"; assert prefix == base; assert os.path.commonpath((prefix, stdlib)) == prefix; assert os.path.commonpath((prefix, module)) == prefix' \
  || { echo "PACKAGE ERROR: finalized package-owned Python smoke failed" >&2; exit 2; }

/bin/cp -R "$PACKAGE_VENV" "$RUNTIME/.venv"
chmod 755 "$RUNTIME/.venv/bin/python"

APP_PYTHON_HOME="$TEMP_ROOT/app-python-home"
mkdir "$APP_PYTHON_HOME"
/usr/bin/env -u PYTHONHOME -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 HOME="$APP_PYTHON_HOME" \
  "$RUNTIME/.venv/bin/python" -c 'import os, platform, sys, sysconfig; prefix=os.path.realpath(sys.prefix); base=os.path.realpath(sys.base_prefix); stdlib=os.path.realpath(sysconfig.get_path("stdlib")); module=os.path.realpath(os.__file__); assert sys.version_info[:2] == (3, 13); assert platform.machine() == "arm64"; assert prefix == base; assert os.path.commonpath((prefix, stdlib)) == prefix; assert os.path.commonpath((prefix, module)) == prefix' \
  || { echo "PACKAGE ERROR: packaged app Python smoke failed" >&2; exit 2; }

PYTHON_MM="$(PYTHONDONTWRITEBYTECODE=1 "$RUNTIME/.venv/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
[[ "$PYTHON_MM" == "3.13" ]] || { echo "PACKAGE ERROR: bundled Python must be 3.13" >&2; exit 2; }
SURFACE="$(PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$SOURCE_ROOT" "$RUNTIME/.venv/bin/python" -c 'import hashlib; from agent_runtime.capability_registry import ADVERTISED_TOOL_NAMES; blob=("\n".join(ADVERTISED_TOOL_NAMES)+"\n").encode(); print(f"{len(ADVERTISED_TOOL_NAMES)}\t{hashlib.sha256(blob).hexdigest()}")')"
IFS=$'\t' read -r PUBLIC_TOOL_COUNT PUBLIC_SURFACE_SHA256 <<< "$SURFACE"
[[ "$PUBLIC_TOOL_COUNT" == "20" && "$PUBLIC_SURFACE_SHA256" =~ ^[0-9a-f]{64}$ ]] \
  || { echo "PACKAGE ERROR: expected public tool surface is not the accepted twenty-tool contract" >&2; exit 2; }

PAYLOAD_PUBLICATION="$("$PYTHON_BIN" "$SOURCE_PACKAGE_ROOT/package_provenance.py" publish-payload \
  "$SOURCE_ROOT/agent_runtime" "$PAYLOADS_ROOT" \
  "$RUNTIME_REVISION" "$RUNTIME_TREE" "$SOURCE_ROOT/requirements.lock" \
  "$PYTHON_MM" "$PUBLIC_TOOL_COUNT" "$PUBLIC_SURFACE_SHA256")" \
  || { echo "PACKAGE ERROR: initial external payload publication failed" >&2; exit 2; }
IFS=$'\t' read -r INITIAL_PAYLOAD_CLOSURE INITIAL_PAYLOAD_RELEASE <<< "$PAYLOAD_PUBLICATION"
[[ "$INITIAL_PAYLOAD_CLOSURE" =~ ^[0-9a-f]{64}$ && -d "$INITIAL_PAYLOAD_RELEASE" ]] \
  || { echo "PACKAGE ERROR: initial payload publication result is invalid" >&2; exit 2; }

sign_adhoc() {
  local identifier="$1"
  shift
  /usr/bin/codesign --force --sign - --identifier "$identifier" \
    -r="designated => identifier \"$identifier\"" "$@" >/dev/null
}

# Sign every native substrate component ad-hoc with a deterministic identifier and requirement.
while IFS= read -r NATIVE_CODE; do
  [[ -n "$NATIVE_CODE" ]] || continue
  if [[ "$NATIVE_CODE" == "$RUNTIME/.venv/bin/python" ]]; then
    CODE_IDENTIFIER="com.picmao.agent-runtime.python"
  else
    RELATIVE_NATIVE="${NATIVE_CODE#"$RUNTIME/"}"
    NATIVE_DIGEST="$(printf '%s' "$RELATIVE_NATIVE" | /usr/bin/shasum -a 256 | /usr/bin/awk '{print $1}')"
    CODE_IDENTIFIER="com.picmao.agent-runtime.native.${NATIVE_DIGEST:0:24}"
  fi
  sign_adhoc "$CODE_IDENTIFIER" "$NATIVE_CODE"
done < <(
  find "$RUNTIME/.venv" -type f -print0 |
    while IFS= read -r -d '' candidate; do
      /usr/bin/file -b "$candidate" | /usr/bin/grep -q 'Mach-O' && printf '%s\n' "$candidate" || true
    done | LC_ALL=C sort
)

sign_adhoc com.picmao.agent-runtime "$MACOS/AgentRuntimeMenuBar"
sign_adhoc com.picmao.agent-runtime.runtime-service "$MACOS/AgentRuntimeRuntimeService"
sign_adhoc com.picmao.agent-runtime.screen-capture "$MACOS/AgentRuntimeScreenCapture"

"$PYTHON_BIN" "$SOURCE_PACKAGE_ROOT/package_provenance.py" substrate-manifest \
  "$RUNTIME" "$RESOURCES/runtime-manifest.json" \
  "$RUNTIME_REVISION" "$RUNTIME_TREE" "$SOURCE_ROOT/requirements.lock" \
  "$PYTHON_MM" "$PUBLIC_TOOL_COUNT" "$PUBLIC_SURFACE_SHA256" \
  || { echo "PACKAGE ERROR: immutable substrate manifest generation failed" >&2; exit 2; }

/usr/bin/plutil -lint "$CONTENTS/Info.plist" >/dev/null
[[ "$(/usr/libexec/PlistBuddy -c 'Print :LSUIElement' "$CONTENTS/Info.plist")" == "true" ]] \
  || { echo "PACKAGE ERROR: LSUIElement must be true" >&2; exit 2; }
/usr/bin/codesign --force --sign - --identifier com.picmao.agent-runtime "$APP" \
  -r='designated => identifier "com.picmao.agent-runtime"' >/dev/null
/usr/bin/codesign --verify --deep --strict "$APP"

"$PYTHON_BIN" "$SOURCE_PACKAGE_ROOT/package_provenance.py" seal-zero-cost \
  "$APP" "$CANDIDATE_HANDOFF" "$INITIAL_PAYLOAD_RELEASE" >/dev/null
PUBLISHED="$("$PYTHON_BIN" "$SOURCE_PACKAGE_ROOT/package_provenance.py" publish-zero-cost \
  "$APP" "$CANDIDATE_HANDOFF" "$INITIAL_PAYLOAD_RELEASE" "$CANDIDATES_ROOT")" \
  || { echo "PACKAGE ERROR: zero-cost distribution publication failed" >&2; exit 2; }
IFS=$'\t' read -r FINAL_APP FINAL_HANDOFF FINAL_PAYLOAD CANDIDATE_SHA256 <<< "$PUBLISHED"
[[ -n "$FINAL_APP" && -n "$FINAL_HANDOFF" && -n "$FINAL_PAYLOAD" && "$CANDIDATE_SHA256" =~ ^[0-9a-f]{64}$ ]] \
  || { echo "PACKAGE ERROR: zero-cost distribution publication result is invalid" >&2; exit 2; }

printf 'candidate_app=%s\n' "$FINAL_APP"
printf 'candidate_handoff=%s\n' "$FINAL_HANDOFF"
printf 'initial_payload_release=%s\n' "$FINAL_PAYLOAD"
printf 'initial_payload_closure=%s\n' "$INITIAL_PAYLOAD_CLOSURE"
printf 'candidate_sha256=%s\n' "$CANDIDATE_SHA256"
