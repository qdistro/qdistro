#!/bin/bash
# Install (or print) the Chromium enterprise policy that force-installs
# qdchrome-extension into every user's profile on this machine.
#
# Per todo/05-system-install-chromium.md. Idempotent.
#
# The default policy directory is /etc/chromium/policies/managed/.
# Pass --browser chrome to write /etc/opt/chrome/policies/managed/,
# or --browser brave for /etc/brave/policies/managed/.
#
# Requires `jq` for safe merge with any existing policy file.
set -euo pipefail

EXT_ID="ammgnkddbnjdhikklpljgiclldedgncf"
CRX_PATH="${QDISTRO_CRX_PATH:-/usr/share/qdistro/extensions/qdistro-chrome.crx}"
UPDATE_XML="${QDISTRO_UPDATE_XML:-/usr/share/qdistro/extensions/qdistro-chrome-update.xml}"
BROWSER="chromium"
MODE="install"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --print)   MODE="print"; shift ;;
        --remove)  MODE="remove"; shift ;;
        --browser) BROWSER="$2"; shift 2 ;;
        -h|--help)
            cat <<EOF
usage: $0 [--print|--remove] [--browser chromium|chrome|brave]

  (default)   install:  write/merge the policy file
  --print     emit the qdistro policy snippet to stdout (no file writes)
  --remove    strip the qdistro entry from an existing policy file
  --browser   which Chromium-family browser (default: chromium)

env:
  QDISTRO_CRX_PATH    absolute path to the packed crx
                      (default: /usr/share/qdistro/extensions/qdistro-chrome.crx)
  QDISTRO_UPDATE_XML  absolute path to the updates.xml
                      (default: /usr/share/qdistro/extensions/qdistro-chrome-update.xml)
EOF
            exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

case "$BROWSER" in
    chromium) POLICY_DIR="/etc/chromium/policies/managed" ;;
    chrome)   POLICY_DIR="/etc/opt/chrome/policies/managed" ;;
    brave)    POLICY_DIR="/etc/brave/policies/managed" ;;
    *) echo "unknown browser: $BROWSER" >&2; exit 2 ;;
esac

POLICY_FILE="$POLICY_DIR/qdistro.json"

if ! command -v jq >/dev/null 2>&1; then
    echo "[install-system-policy] jq is required" >&2
    exit 1
fi

qdistro_block() {
    cat <<JSON
{
  "ExtensionInstallForcelist": [
    "${EXT_ID};file://${UPDATE_XML}"
  ],
  "ExtensionSettings": {
    "${EXT_ID}": {
      "installation_mode": "force_installed",
      "update_url": "file://${UPDATE_XML}",
      "toolbar_pin": "force_pinned"
    }
  }
}
JSON
}

case "$MODE" in
    print)
        qdistro_block
        ;;
    install)
        mkdir -p "$POLICY_DIR"
        if [[ -f "$POLICY_FILE" ]]; then
            tmp=$(mktemp)
            # Merge: union ExtensionInstallForcelist, replace
            # ExtensionSettings entry for our id only.
            jq --slurp '
                .[0] as $old | .[1] as $new
                | $old
                | .ExtensionInstallForcelist =
                    (((.ExtensionInstallForcelist // [])
                      + $new.ExtensionInstallForcelist)
                     | unique)
                | .ExtensionSettings =
                    ((.ExtensionSettings // {}) + $new.ExtensionSettings)
            ' "$POLICY_FILE" <(qdistro_block) > "$tmp"
            mv "$tmp" "$POLICY_FILE"
        else
            qdistro_block > "$POLICY_FILE"
        fi
        chmod 644 "$POLICY_FILE"
        echo "[install-system-policy] wrote $POLICY_FILE"
        ;;
    remove)
        if [[ ! -f "$POLICY_FILE" ]]; then
            echo "[install-system-policy] $POLICY_FILE not present; nothing to remove"
            exit 0
        fi
        tmp=$(mktemp)
        jq --arg id "$EXT_ID" '
            if .ExtensionInstallForcelist then
                .ExtensionInstallForcelist |=
                    map(select(test("^" + $id + ";") | not))
            else . end
            | if .ExtensionSettings then
                  .ExtensionSettings |= del(.[$id])
              else . end
        ' "$POLICY_FILE" > "$tmp"
        mv "$tmp" "$POLICY_FILE"
        echo "[install-system-policy] removed $EXT_ID entry from $POLICY_FILE"
        ;;
esac
