#!/bin/bash
# Build + install the qdistro_presentation SELinux policy module.
set -euo pipefail

DIR=$(cd "$(dirname "$0")" && pwd)
cd "$DIR"

if [ ! -d /usr/share/selinux/devel ]; then
    echo "[presentation-policy-install] SKIP: /usr/share/selinux/devel missing" \
        "(install selinux-policy-devel)"
    exit 0
fi

if ! command -v semodule >/dev/null 2>&1; then
    echo "[presentation-policy-install] FAIL: semodule not installed" >&2
    exit 1
fi

INCLUDE_DIR=/usr/share/selinux/devel/include/contrib
mkdir -p "$INCLUDE_DIR"
install -m 0644 qdistro_presentation.if "$INCLUDE_DIR/qdistro_presentation.if"

make MODULE=qdistro_presentation
semodule -i qdistro_presentation.pp

if ! semodule -l | grep -q '^qdistro_presentation\b'; then
    echo "[presentation-policy-install] FAIL: qdistro_presentation not listed" >&2
    exit 2
fi

if [ -d /var/lib/qdistro/presentation ]; then
    restorecon -RF /var/lib/qdistro/presentation || true
fi

echo "[presentation-policy-install] OK — qdistro_presentation active"
