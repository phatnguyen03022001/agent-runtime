#!/usr/bin/env bash
set -euo pipefail

PACKAGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$PACKAGE_ROOT/.." && pwd -P)"
APP="$REPO_ROOT/build/Agent Runtime.app"
CONTENTS="$APP/Contents"
MACOS="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"

/usr/bin/xcrun swift build --package-path "$PACKAGE_ROOT" -c release
BIN_DIR="$(/usr/bin/xcrun swift build --package-path "$PACKAGE_ROOT" -c release --show-bin-path)"
BINARY="$BIN_DIR/AgentRuntimeMenuBar"
[[ -x "$BINARY" ]] || { echo "PACKAGE ERROR: missing AgentRuntimeMenuBar binary" >&2; exit 2; }

rm -rf "$APP"
mkdir -p "$MACOS" "$RESOURCES"
cp "$PACKAGE_ROOT/AppBundle/Info.plist" "$CONTENTS/Info.plist"
cp "$BINARY" "$MACOS/AgentRuntimeMenuBar"
printf '%s\n' "$REPO_ROOT" > "$RESOURCES/checkout-path.txt"
chmod 755 "$MACOS/AgentRuntimeMenuBar"

/usr/bin/plutil -lint "$CONTENTS/Info.plist" >/dev/null
[[ "$(/usr/libexec/PlistBuddy -c 'Print :LSUIElement' "$CONTENTS/Info.plist")" == "true" ]] \
  || { echo "PACKAGE ERROR: LSUIElement must be true" >&2; exit 2; }
/usr/bin/codesign --force --deep --sign - "$APP" >/dev/null
/usr/bin/codesign --verify --deep --strict "$APP"

echo "$APP"
