#!/bin/bash
# Build the qdistro browser-bridge WebExtension into per-browser
# layouts under dist/. Phase-8 MVP — unsigned. AMO signing is a
# release-time step (spec/14 §"Distribution constraints").
#
# Outputs:
#   dist/firefox/        unpacked tree with manifest.json (MV2)
#   dist/chromium/       unpacked tree with manifest.json (MV3)
#   dist/qdistro-firefox.xpi    zip of dist/firefox/
#   dist/qdistro-chromium.zip   zip of dist/chromium/
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DIST="$HERE/dist"

rm -rf "$DIST"
mkdir -p "$DIST/firefox" "$DIST/chromium"

# Firefox MV2 layout.
cp "$HERE/manifest.firefox.json"  "$DIST/firefox/manifest.json"
cp "$HERE/popup.html"             "$DIST/firefox/"
cp "$HERE/popup.js"               "$DIST/firefox/"
cp "$HERE/background.js"          "$DIST/firefox/"
cp "$HERE/content.js"             "$DIST/firefox/"
[ -d "$HERE/icons" ] && cp -r "$HERE/icons" "$DIST/firefox/"

# Chromium MV3 layout.
cp "$HERE/manifest.chromium.json" "$DIST/chromium/manifest.json"
cp "$HERE/popup.html"             "$DIST/chromium/"
cp "$HERE/popup.js"               "$DIST/chromium/"
cp "$HERE/background.js"          "$DIST/chromium/"
cp "$HERE/content.js"             "$DIST/chromium/"
[ -d "$HERE/icons" ] && cp -r "$HERE/icons" "$DIST/chromium/"

# Zip both up. zip is in the standard Tumbleweed image; bail if
# missing rather than silently producing only the unpacked trees.
if ! command -v zip >/dev/null 2>&1; then
    echo "[build-extension] zip not installed; unpacked trees written" >&2
    exit 0
fi
( cd "$DIST/firefox"  && zip -qr "$DIST/qdistro-firefox.xpi"   . )
( cd "$DIST/chromium" && zip -qr "$DIST/qdistro-chromium.zip"  . )

echo "[build-extension] OK"
ls -la "$DIST"
