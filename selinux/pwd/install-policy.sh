#!/bin/bash
# Build + install the qdistro_pwd SELinux policy module on a fresh
# clone. Idempotent.
#
# Module 0.2.0 (Phase 2, this revision): enforcing — permissive tag
# dropped after audit2allow harvest from s60 + s61 + portal-keys
# vault flow on val-260430. Allow set extended for cert_t (Python
# ssl module init), cgroup_t (/proc/<pid>/cgroup reads), and
# self:capability sys_ptrace (cross-uid /proc/<other-uid>/exe).
#
# Pre-reqs: selinux-policy-devel + checkpolicy + policycoreutils
# (Tumbleweed: zypper install selinux-policy-devel checkpolicy policycoreutils).
#
# Order: install qdistro_pwd AFTER qdistro_broker (the broker .if is
# referenced indirectly via Tumbleweed's contrib include path; install
# both in either order works because the pwd module doesn't gen_require
# any qdistro_broker types).
#
# SPDX-License-Identifier: MIT
set -euo pipefail

DIR=$(cd "$(dirname "$0")" && pwd)
cd "$DIR"

if [ ! -d /usr/share/selinux/devel ]; then
    echo "[pwd-policy-install] SKIP: /usr/share/selinux/devel missing" \
        "(install selinux-policy-devel)"
    exit 0
fi

if ! command -v semodule >/dev/null 2>&1; then
    echo "[pwd-policy-install] FAIL: semodule not installed" >&2
    exit 1
fi

# Drop the .if into the contrib include dir BEFORE building so other
# modules can call qdistro_pwd_dbus_chat / qdistro_pwd_read_audit.
INCLUDE_DIR=/usr/share/selinux/devel/include/contrib
mkdir -p "$INCLUDE_DIR"
install -m 0644 qdistro_pwd.if "$INCLUDE_DIR/qdistro_pwd.if"

make MODULE=qdistro_pwd
semodule -i qdistro_pwd.pp

if ! semodule -l | grep -q '^qdistro_pwd\b'; then
    echo "[pwd-policy-install] FAIL: qdistro_pwd not listed by semodule -l" >&2
    exit 2
fi

# Relabel the daemon exec + vault dirs if they exist so the new
# transition + file labels take effect immediately.
PWD_PY=/usr/libexec/qdistro/qdistro_pwd_daemon.py
if [ -f "$PWD_PY" ] && command -v restorecon >/dev/null 2>&1; then
    restorecon "$PWD_PY" 2>/dev/null \
        || (command -v chcon >/dev/null 2>&1 \
            && chcon -t qdistro_pwd_exec_t "$PWD_PY" 2>/dev/null) \
        || true
    restorecon -R /var/lib/qdistro/vaults 2>/dev/null || true
    restorecon -R /var/lib/qdistro/audit  2>/dev/null || true
    if command -v systemctl >/dev/null 2>&1 && \
       systemctl is-active --quiet qdistro-pwd.service 2>/dev/null; then
        systemctl restart qdistro-pwd.service 2>/dev/null || true
    fi
fi

echo "[pwd-policy-install] OK — qdistro_pwd active (enforcing, 0.2.0)"
echo "[pwd-policy-install] interface dropped at $INCLUDE_DIR/qdistro_pwd.if"
