#!/bin/bash
# Install (or print) the Firefox enterprise policy that force-installs
# qdistro-firefox-extension into every user's profile on this machine.
#
# Per todo/01-system-install-firefox.md. Idempotent. Default mode
# writes the merged policy file; --print emits to stdout; --remove
# strips the qdistro entry from an existing policy file.
#
# Writes to /etc/firefox/policies/policies.json by default. Other
# Firefox layouts may want /usr/lib64/firefox/distribution/policies.json
# — pass --path <dir> to override.
#
# Requires `jq` for safe merge with any existing policy file.
set -euo pipefail

EXT_ID="qdistro-firefox@qdistro.local"
XPI_PATH="${QDISTRO_XPI_PATH:-/usr/share/qdistro/extensions/qdistro-firefox.xpi}"
POLICY_DIR="/etc/firefox/policies"
MODE="install"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --print)  MODE="print"; shift ;;
        --remove) MODE="remove"; shift ;;
        --path)   POLICY_DIR="$2"; shift 2 ;;
        -h|--help)
            cat <<EOF
usage: $0 [--print|--remove] [--path <dir>]

  (default)   install: write/merge the policy at <POLICY_DIR>/policies.json
  --print     emit the qdistro policy snippet to stdout (no file writes)
  --remove    remove the qdistro entry from an existing policy file
  --path DIR  override policy directory (default: /etc/firefox/policies)

env:
  QDISTRO_XPI_PATH  absolute path to the signed xpi
                    (default: /usr/share/qdistro/extensions/qdistro-firefox.xpi)
EOF
            exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

if ! command -v jq >/dev/null 2>&1; then
    echo "[install-system-policy] jq is required for safe merge" >&2
    exit 1
fi

POLICY_FILE="$POLICY_DIR/policies.json"

qdistro_block() {
    cat <<JSON
{
  "policies": {
    "ExtensionSettings": {
      "${EXT_ID}": {
        "installation_mode": "force_installed",
        "install_url": "file://${XPI_PATH}",
        "default_area": "navbar"
      }
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
            # Merge: keep any existing policies, overwrite our entry only.
            tmp=$(mktemp)
            jq --slurp '.[0] * .[1]' \
                "$POLICY_FILE" <(qdistro_block) \
                > "$tmp"
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
        jq --arg id "$EXT_ID" \
            'if .policies.ExtensionSettings then
                .policies.ExtensionSettings |= (del(.[$id])
                | if length == 0 then null else . end)
              | if .policies.ExtensionSettings == null
                  then del(.policies.ExtensionSettings) else . end
              | if (.policies | length) == 0 then {} else . end
             else . end' \
            "$POLICY_FILE" > "$tmp"
        mv "$tmp" "$POLICY_FILE"
        echo "[install-system-policy] removed $EXT_ID entry from $POLICY_FILE"
        ;;
esac
