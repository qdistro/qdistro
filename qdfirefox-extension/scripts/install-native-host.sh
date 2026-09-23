#!/bin/bash
# Install (or print) the Firefox native-messaging-host manifest for
# the qdistro bridge.
#
# Firefox's native-host manifest lives at one of:
#   user:     ~/.mozilla/native-messaging-hosts/<name>.json
#   system:   /usr/lib/mozilla/native-messaging-hosts/<name>.json
#             /usr/lib64/mozilla/native-messaging-hosts/<name>.json
#
# This script installs the user-level manifest by default. Pass
# --system to write the system-level one (requires root).
#
# Differences from Chromium's manifest:
#   - field is `allowed_extensions` (not `allowed_origins`)
#   - values are extension IDs (gecko.id) not chrome-extension:// URIs
#
# Required environment / arguments:
#   QDISTRO_BRIDGE_PATH  absolute path to the qdistro-browser-bridge
#                        executable (the Python entry point). If
#                        unset, defaults to $(command -v
#                        qdistro-browser-bridge).
set -euo pipefail

HOST_NAME="qdistro"
EXT_ID="qdistro-firefox@qdistro.local"
# Resolve the bridge against a PACKAGED install first, then a dev checkout:
#   1. $QDISTRO_BRIDGE_PATH (explicit wins)
#   2. /usr/lib/qdistro/browser-bridge — where the qdistro installer actually
#      puts the native-messaging host. NOTE: no `qdistro-browser-bridge` binary
#      is ever installed on PATH (the installed CLI is qdistro-browser-install),
#      so the old PATH-only lookup failed on every real install and this script
#      only worked when the caller happened to set the env var by hand.
#   3. `qdistro-browser-bridge` on PATH, for a dev tree that exports one.
# Overridable ONLY so the test suite can exercise the precedence branch on a
# host with no qdistro install; production callers use QDISTRO_BRIDGE_PATH.
DEFAULT_BRIDGE_PATH="${QDISTRO_DEFAULT_BRIDGE_PATH:-/usr/lib/qdistro/browser-bridge}"
BRIDGE_PATH="${QDISTRO_BRIDGE_PATH:-}"
if [[ -z "$BRIDGE_PATH" && -x "$DEFAULT_BRIDGE_PATH" ]]; then
    BRIDGE_PATH="$DEFAULT_BRIDGE_PATH"
fi
if [[ -z "$BRIDGE_PATH" ]]; then
    BRIDGE_PATH="$(command -v qdistro-browser-bridge 2>/dev/null || true)"
fi

if [[ -z "$BRIDGE_PATH" ]]; then
    echo "[install-native-host] no bridge found: set QDISTRO_BRIDGE_PATH, or install qdistro (expected $DEFAULT_BRIDGE_PATH)" >&2
    exit 1
fi

MODE="user"
case "${1:-}" in
    --system) MODE="system" ;;
    --print)  MODE="print" ;;
    "")       ;;
    *) echo "usage: $0 [--system|--print]" >&2; exit 2 ;;
esac

manifest_json() {
    cat <<JSON
{
  "name": "${HOST_NAME}",
  "description": "qdistro browser bridge (native messaging host)",
  "path": "${BRIDGE_PATH}",
  "type": "stdio",
  "allowed_extensions": ["${EXT_ID}"]
}
JSON
}

case "$MODE" in
    print)
        manifest_json
        ;;
    user)
        DIR="$HOME/.mozilla/native-messaging-hosts"
        mkdir -p "$DIR"
        manifest_json > "$DIR/${HOST_NAME}.json"
        echo "[install-native-host] wrote $DIR/${HOST_NAME}.json"
        ;;
    system)
        if [[ -d /usr/lib64/mozilla/native-messaging-hosts ]]; then
            DIR=/usr/lib64/mozilla/native-messaging-hosts
        else
            DIR=/usr/lib/mozilla/native-messaging-hosts
        fi
        mkdir -p "$DIR"
        manifest_json > "$DIR/${HOST_NAME}.json"
        echo "[install-native-host] wrote $DIR/${HOST_NAME}.json"
        ;;
esac
