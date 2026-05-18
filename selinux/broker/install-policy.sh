#!/bin/bash
# Build + install the qdistro_broker SELinux policy module on a
# fresh clone. Idempotent.
#
# Module 0.2.0 (Phase 2): labels the broker py file as
# qdistro_broker_exec_t and adds the init_daemon_domain transition
# so systemd-started broker runs as qdistro_broker_t. Domain is
# permissive for the rollout — Phase 3 will collect AVCs and remove
# the permissive tag. (Module 0.3.2 since 2026-04-29 — broker path
# moved from /usr/local/lib/qdistro/ to /usr/libexec/qdistro/ so the
# .fc rule wins natively against Tumbleweed's lib_t glob.)
#
# Pre-reqs: selinux-policy-devel + checkpolicy + policycoreutils
# (Tumbleweed: zypper install selinux-policy-devel checkpolicy policycoreutils).
#
# Order: install qdistro_broker BEFORE qdistro_tier1; tier1 calls
# qdistro_broker_dbus_chat from broker.if which gen_requires a type
# from qdistro_broker.te. semodule rejects loading a module whose
# gen_required types are absent.
#
# SPDX-License-Identifier: MIT
set -euo pipefail

DIR=$(cd "$(dirname "$0")" && pwd)
cd "$DIR"

if [ ! -d /usr/share/selinux/devel ]; then
    echo "[broker-policy-install] SKIP: /usr/share/selinux/devel missing" \
        "(install selinux-policy-devel)"
    exit 0
fi

if ! command -v semodule >/dev/null 2>&1; then
    echo "[broker-policy-install] FAIL: semodule not installed" >&2
    exit 1
fi

# Dependency: qdistro_broker.te's 0.4.0 gen_require pulls in
# qdistro_pwd_audit_t (declared in the pwd module's .te). semodule's
# AST resolution at install time requires that type to be present in
# the active policy store — gen_require alone doesn't define it, it
# only references it. If qdistro_pwd isn't loaded yet, semodule fails
# with `Failed to resolve typeattributeset statement at ...cil:54` /
# `Failed to resolve AST`. Install pwd first when its installer is
# present and the module is not already active. Idempotent re-run is
# safe (the pwd installer itself is idempotent).
if ! semodule -l 2>/dev/null | grep -q '^qdistro_pwd\b'; then
    PWD_INSTALLER="$DIR/../pwd/install-policy.sh"
    if [ -x "$PWD_INSTALLER" ]; then
        echo "[broker-policy-install] qdistro_pwd not loaded — installing it first"
        (cd "$(dirname "$PWD_INSTALLER")" && bash install-policy.sh) \
            || { echo "[broker-policy-install] FAIL: prereq qdistro_pwd install failed" >&2; exit 3; }
    fi
fi

# Drop the latest .if into the contrib include dir BEFORE building
# our own .pp. checkmodule glob-includes every .if from
# $DEVEL/include/contrib/ when expanding any module — including
# OURS. If a previous bootstrap left a syntactically-broken .if
# there (e.g. a Phase 1→Phase 2 transition with an obsolete
# gen_require body), our own `make` would fail to recover from it
# without the .if drop happening first. Doing this before make
# breaks the chicken-and-egg.
INCLUDE_DIR=/usr/share/selinux/devel/include/contrib
mkdir -p "$INCLUDE_DIR"
install -m 0644 qdistro_broker.if "$INCLUDE_DIR/qdistro_broker.if"

# Build .pp from .te/.if/.fc.
make MODULE=qdistro_broker

# Idempotent install — semodule -i replaces an existing module.
semodule -i qdistro_broker.pp

if ! semodule -l | grep -q '^qdistro_broker\b'; then
    echo "[broker-policy-install] FAIL: qdistro_broker not listed by semodule -l" >&2
    exit 2
fi

# The pre-make drop above already exposed qdistro_broker_dbus_chat()
# (and the future _read_runtime interface) callable from other policy
# modules at compile time via $INCLUDE_DIR. Nothing else to do here
# now that the .if drop happens before `make`.

# Phase 2: relabel the broker exec if it's already installed and
# restart the service so the new domain transition takes effect.
# Best-effort — both branches are no-ops on environments without the
# broker service set up yet (e.g., during the very first
# fresh-vm-bootstrap.sh pass before install-broker-for-qdwin.sh runs).
#
# Since 2026-04-29 the broker installs to /usr/libexec/qdistro/ which
# is outside Tumbleweed's `/usr/(.*/)?lib(/.*)?` → `lib_t` glob, so
# restorecon now picks our .fc entry deterministically. chcon kept as
# fallback for hosts where the .fc hasn't been refreshed yet.
BROKER_PY=/usr/libexec/qdistro/qdistro_admin_broker.py
if [ -f "$BROKER_PY" ] && command -v restorecon >/dev/null 2>&1; then
    restorecon "$BROKER_PY" 2>/dev/null \
        || (command -v chcon >/dev/null 2>&1 \
            && chcon -t qdistro_broker_exec_t "$BROKER_PY" 2>/dev/null) \
        || true
    if command -v systemctl >/dev/null 2>&1 && \
       systemctl is-active --quiet qdistro-admin-broker.service 2>/dev/null; then
        systemctl restart qdistro-admin-broker.service 2>/dev/null || true
    fi
fi

echo "[broker-policy-install] OK — qdistro_broker active"
echo "[broker-policy-install] interface dropped at $INCLUDE_DIR/qdistro_broker.if"
