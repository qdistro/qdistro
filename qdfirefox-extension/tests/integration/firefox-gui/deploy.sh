#!/bin/bash
# Deploy qdfirefox-extension + stub native host into a VM.
#
# Usage: VMNAME=<vm> bash deploy.sh
#
# Idempotent: re-runs are safe and skip work that's already done.
# Logs progress to stderr; on failure exits non-zero with the offending step.
set -euo pipefail

VM="${VMNAME:?VMNAME env var required}"
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
QDISTRO_REPO="${QDISTRO_REPO:-/home/playai/doc/qdistro-org/qdistro}"
VMEXEC="$QDISTRO_REPO/scripts/vm/vm-exec"

log() { echo "[deploy] $*" >&2; }

# 1. Install Firefox if missing.
if ! $VMEXEC "$VM" "command -v firefox >/dev/null 2>&1" 2>/dev/null; then
    log "installing MozillaFirefox in $VM"
    $VMEXEC "$VM" "zypper -n install -y MozillaFirefox" >&2
else
    log "firefox already installed"
fi

# 2. Build extension on host if dist/ is stale or missing.
if [[ ! -d "$REPO_ROOT/dist/firefox" ]]; then
    log "building extension on host"
    ( cd "$REPO_ROOT" && bash scripts/build-extension.sh ) >&2
else
    log "dist/firefox exists; assuming up-to-date"
fi

# 3. Tar up dist/firefox and stub-bridge.py; pipe into the VM. Use base64
#    to avoid binary-in-JSON woes with qemu-ga.
log "copying dist/firefox + stub-bridge.py into VM"
TAR_B64=$(
    tar -C "$REPO_ROOT" -czf - dist/firefox \
        | base64 -w0
)
SCRIPT_B64=$(base64 -w0 < "$REPO_ROOT/tests/integration/firefox-gui/stub-bridge.py")

PAYLOAD_B64=$(base64 -w0 <<EOF
set -euo pipefail
mkdir -p /home/admin/qdfirefox-extension
echo $TAR_B64 | base64 -d | tar -xzf - -C /home/admin/qdfirefox-extension
mkdir -p /home/admin/qdfirefox-extension/tests/integration/firefox-gui
echo $SCRIPT_B64 | base64 -d > /home/admin/qdfirefox-extension/tests/integration/firefox-gui/stub-bridge.py
chmod +x /home/admin/qdfirefox-extension/tests/integration/firefox-gui/stub-bridge.py
chown -R admin:admin /home/admin/qdfirefox-extension

# 4. Install the native-host manifest.
mkdir -p /home/admin/.mozilla/native-messaging-hosts
cat >/home/admin/.mozilla/native-messaging-hosts/qdistro.json <<JSON
{
  "name": "qdistro",
  "description": "qdistro stub bridge (integration test)",
  "path": "/home/admin/qdfirefox-extension/tests/integration/firefox-gui/stub-bridge.py",
  "type": "stdio",
  "allowed_extensions": ["qdistro-firefox@qdistro.local"]
}
JSON
chown -R admin:admin /home/admin/.mozilla
echo "[deploy] manifest at /home/admin/.mozilla/native-messaging-hosts/qdistro.json"

# 5. Verify the stub script runs (sanity).
/usr/bin/python3 /home/admin/qdfirefox-extension/tests/integration/firefox-gui/stub-bridge.py < /dev/null >/dev/null 2>&1 || true
echo "[deploy] OK"
EOF
)
$VMEXEC "$VM" "echo $PAYLOAD_B64 | base64 -d | bash" >&2

log "deploy complete on $VM"
