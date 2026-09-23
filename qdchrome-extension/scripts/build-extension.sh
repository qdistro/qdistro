#!/bin/bash
# Build qdchrome-extension into the Chromium MV3 layout under dist/.
#
# Outputs:
#   dist/chromium/        unpacked MV3 tree (uses importScripts)
#   dist/chromium.zip     packed for `chrome.runtime.installFromZip`
#
# This repo is Chromium-only. The Firefox extension has exactly one
# canonical source and is NOT built here:
#   ../qdfirefox-extension   (id qdistro-firefox@qdistro.local)
#   -- the qdistro/browser_bridge/extension "bundled" tree that used to be
#      the other Firefox source was deleted for J11 (no origin allowlist)
#      and its id qdistro@qdistro.local is revoked by the bridge.
# A Firefox MV2 build used to be emitted here under id qdistro@qdistro.local,
# which collided with the then-bundled qdistro/browser_bridge/extension (two
# distinct codebases, same gecko id). The target was removed to canonicalize
# the Firefox artifacts; that bundled tree was later deleted outright (J11 —
# it had no origin gate) and its id is now revoked by the bridge.
# See ../qdistro/doc/browser.md ("Firefox extension artifacts").
#
# Chromium MV3 uses importScripts(...) in the service worker so we
# can ship the source tree as-is.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$HERE/dist"

rm -rf "$DIST"
mkdir -p "$DIST/chromium/src/modules" "$DIST/chromium/src/content"

# --- Chromium MV3 -----------------------------------------------------
cp "$HERE/manifest.chromium.json" "$DIST/chromium/manifest.json"
# The service worker (background.js) lives at the extension root; its
# importScripts paths are relative to that root.
cp "$HERE/src/background.js" "$DIST/chromium/background.js"
cp "$HERE/src/popup.html"    "$DIST/chromium/popup.html"
cp "$HERE/src/popup.js"      "$DIST/chromium/popup.js"
cp "$HERE/src/options.html"  "$DIST/chromium/options.html"
cp "$HERE/src/options.js"    "$DIST/chromium/options.js"
cp "$HERE/src/api.js"        "$DIST/chromium/src/api.js"
cp "$HERE/src/port.js"       "$DIST/chromium/src/port.js"
cp "$HERE/src/dispatcher.js" "$DIST/chromium/src/dispatcher.js"
cp "$HERE/src/intent.js"     "$DIST/chromium/src/intent.js"
cp "$HERE/src/gate.js"       "$DIST/chromium/src/gate.js"
cp "$HERE"/src/modules/*.js  "$DIST/chromium/src/modules/"
cp "$HERE"/src/content/*.js  "$DIST/chromium/src/content/"
cp -r "$HERE/icons"          "$DIST/chromium/icons"

# --- Zip --------------------------------------------------------------
if ! command -v zip >/dev/null 2>&1; then
    echo "[build-extension] zip not installed; unpacked tree written" >&2
    exit 0
fi
( cd "$DIST/chromium" && zip -qr "$DIST/chromium.zip" . )

echo "[build-extension] OK"
ls -la "$DIST"
