#!/bin/bash
# Build qdfirefox-extension into dist/firefox/ and dist/firefox.xpi.
#
# Firefox MV3 uses a flat scripts-array background, so we ship the
# source tree as-is — no concatenation needed, unlike the MV2 path
# in qdchrome-extension. The xpi is just a zip of the source tree
# with manifest.json at the root.
#
# Flags:
#   --sign   AMO-sign the xpi via web-ext (unlisted channel). Requires
#            WEB_EXT_API_KEY and WEB_EXT_API_SECRET in the env; without
#            them the flag is a no-op (warns and skips). Produces
#            dist/firefox-signed.xpi alongside the unsigned xpi.
set -euo pipefail

SIGN=0
for arg in "$@"; do
    case "$arg" in
        --sign) SIGN=1 ;;
        *) echo "[build-extension] unknown flag: $arg" >&2; exit 2 ;;
    esac
done

HERE="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$HERE/dist"
OUT="$DIST/firefox"

rm -rf "$DIST"
mkdir -p "$OUT/src/modules" "$OUT/src/content" "$OUT/icons"

cp "$HERE/manifest.json"        "$OUT/manifest.json"
cp "$HERE/src/api.js"           "$OUT/src/api.js"
cp "$HERE/src/port.js"          "$OUT/src/port.js"
cp "$HERE/src/dispatcher.js"    "$OUT/src/dispatcher.js"
cp "$HERE/src/intent.js"        "$OUT/src/intent.js"
cp "$HERE/src/gate.js"          "$OUT/src/gate.js"
cp "$HERE/src/background.js"    "$OUT/src/background.js"
cp "$HERE/src/popup.html"       "$OUT/src/popup.html"
cp "$HERE/src/popup.js"         "$OUT/src/popup.js"
cp "$HERE/src/options.html"     "$OUT/src/options.html"
cp "$HERE/src/options.js"       "$OUT/src/options.js"
cp "$HERE"/src/modules/*.js     "$OUT/src/modules/"
cp "$HERE"/src/content/*.js     "$OUT/src/content/"
cp "$HERE"/icons/*              "$OUT/icons/"

if ! command -v zip >/dev/null 2>&1; then
    echo "[build-extension] zip not installed; unpacked tree at $OUT" >&2
    exit 0
fi
( cd "$OUT" && zip -qr "$DIST/firefox.xpi" . )

if [ "$SIGN" -eq 1 ]; then
    if [ -z "${WEB_EXT_API_KEY:-}" ] || [ -z "${WEB_EXT_API_SECRET:-}" ]; then
        echo "[build-extension] --sign skipped: WEB_EXT_API_KEY / WEB_EXT_API_SECRET not set" >&2
    elif ! command -v web-ext >/dev/null 2>&1; then
        echo "[build-extension] --sign skipped: web-ext not installed (npm i -g web-ext)" >&2
    else
        echo "[build-extension] signing via web-ext (unlisted channel) ..."
        # web-ext writes the signed xpi to --artifacts-dir; rename so
        # downstream consumers find a stable path.
        SIGN_TMP="$DIST/_signed"
        mkdir -p "$SIGN_TMP"
        web-ext sign \
            --api-key="$WEB_EXT_API_KEY" \
            --api-secret="$WEB_EXT_API_SECRET" \
            --channel=unlisted \
            --source-dir="$OUT" \
            --artifacts-dir="$SIGN_TMP"
        signed=$(ls "$SIGN_TMP"/*.xpi 2>/dev/null | head -n1)
        if [ -n "$signed" ]; then
            mv "$signed" "$DIST/firefox-signed.xpi"
            rmdir "$SIGN_TMP" 2>/dev/null || true
            echo "[build-extension] signed xpi: $DIST/firefox-signed.xpi"
        else
            echo "[build-extension] web-ext sign produced no xpi" >&2
            exit 1
        fi
    fi
fi

echo "[build-extension] OK"
ls -la "$DIST"
