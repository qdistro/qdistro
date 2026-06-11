#!/bin/bash
# Build + install the qdistro_session_manager SELinux policy module on a
# fresh clone. Idempotent.
#
# Module 0.1.0 (Phase 1): PERMISSIVE rollout. Establishes the
# qdistro_sessmgr_t domain + exec transition so the per-silo WireGuard
# private keys in qdistro-pwd can be PINNED to the session-manager's
# SELinux label (todo/fable-networking Opt 3-B), tightening custody
# beyond pin_uid=0. The daemon's own denials are logged, not enforced,
# so this rollout cannot wedge the lifecycle TCB; the enforcing allow
# set + dropping the permissive tag is Phase 2 (audit2allow harvest).
#
# Pre-reqs: selinux-policy-devel + checkpolicy + policycoreutils
# (Tumbleweed: zypper install selinux-policy-devel checkpolicy policycoreutils).
#
# Order: install AFTER qdistro_pwd so the qdistro_pwd.if interface is
# present in the contrib include dir (this module optional_policy-calls
# qdistro_pwd_dbus_chat). It is wrapped in optional_policy, so building
# without pwd still succeeds — the pwd client edge just won't be granted.
#
# SPDX-License-Identifier: MIT
set -euo pipefail

DIR=$(cd "$(dirname "$0")" && pwd)
cd "$DIR"

if [ ! -d /usr/share/selinux/devel ]; then
    echo "[sessmgr-policy-install] SKIP: /usr/share/selinux/devel missing" \
        "(install selinux-policy-devel)"
    exit 0
fi

if ! command -v semodule >/dev/null 2>&1; then
    echo "[sessmgr-policy-install] FAIL: semodule not installed" >&2
    exit 1
fi

# Drop the .if into the contrib include dir so other modules can call
# qdistro_sessmgr_dbus_chat.
INCLUDE_DIR=/usr/share/selinux/devel/include/contrib
mkdir -p "$INCLUDE_DIR"
install -m 0644 qdistro_session_manager.if \
    "$INCLUDE_DIR/qdistro_session_manager.if"

make MODULE=qdistro_session_manager
semodule -i qdistro_session_manager.pp

if ! semodule -l | grep -q '^qdistro_session_manager\b'; then
    echo "[sessmgr-policy-install] FAIL: qdistro_session_manager not listed" \
        "by semodule -l" >&2
    exit 2
fi

# Relabel the daemon exec if present so the new transition takes effect
# immediately, then restart so the running daemon picks up the domain.
SM_PY=/usr/libexec/qdistro/qdistro_session_manager.py
if [ -f "$SM_PY" ] && command -v restorecon >/dev/null 2>&1; then
    restorecon "$SM_PY" 2>/dev/null \
        || (command -v chcon >/dev/null 2>&1 \
            && chcon -t qdistro_sessmgr_exec_t "$SM_PY" 2>/dev/null) \
        || true
    if command -v systemctl >/dev/null 2>&1 && \
       systemctl is-active --quiet qdistro-session-manager.service 2>/dev/null; then
        systemctl restart qdistro-session-manager.service 2>/dev/null || true
    fi
fi

echo "[sessmgr-policy-install] OK — qdistro_session_manager active" \
     "(PERMISSIVE, 0.1.0)"
echo "[sessmgr-policy-install] pin label for wg keys:" \
     "system_u:system_r:qdistro_sessmgr_t:s0"
echo "[sessmgr-policy-install] interface dropped at" \
     "$INCLUDE_DIR/qdistro_session_manager.if"
