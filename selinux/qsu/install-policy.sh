#!/bin/bash
# Build + install the qdistro_qsu SELinux policy module on a fresh clone.
# Idempotent.
#
# Pre-reqs: selinux-policy-devel + checkpolicy + policycoreutils.
# Tumbleweed: `zypper install selinux-policy-devel checkpolicy policycoreutils`.
#
# This confines qdistro-root-exec.service (the qsu privileged-exec
# delegator) into qdistro_root_exec_t and gives the setuid child a
# qsu_child_t transition. It must be installed AFTER the script is in
# place at /usr/local/lib/qdistro/qdistro_root_exec.py (the .fc labels it)
# and AFTER the qdistro_broker module is active (this module calls
# qdistro_broker_dbus_chat() and gen_requires qdistro_broker_t).
#
# SPDX-License-Identifier: MIT
set -euo pipefail

DIR=$(cd "$(dirname "$0")" && pwd)
cd "$DIR"

if [ ! -d /usr/share/selinux/devel ]; then
    echo "[qsu-install] SKIP: /usr/share/selinux/devel missing" \
        "(install selinux-policy-devel)"
    exit 0
fi

if ! command -v semodule >/dev/null 2>&1; then
    echo "[qsu-install] FAIL: semodule not installed" >&2
    exit 1
fi

# Dependency: the broker module must be present in the active store first
# (qdistro_qsu.te gen_requires qdistro_broker_t and calls
# qdistro_broker_dbus_chat()). Install it if absent. Idempotent re-run is
# safe (the broker installer is itself idempotent).
if ! semodule -l 2>/dev/null | grep -q '^qdistro_broker\b'; then
    BROKER_INSTALLER="$DIR/../broker/install-policy.sh"
    if [ -x "$BROKER_INSTALLER" ]; then
        echo "[qsu-install] qdistro_broker not loaded — installing it first"
        (cd "$(dirname "$BROKER_INSTALLER")" && bash install-policy.sh) \
            || { echo "[qsu-install] FAIL: prereq qdistro_broker install failed" >&2; exit 3; }
    fi
fi

# Build .pp from .te/.if/.fc via the system devel Makefile (checkmodule +
# semodule_package under the hood, with the refpolicy m4 macros expanded).
make MODULE=qdistro_qsu

# Idempotent install — semodule -i replaces an existing module.
semodule -i qdistro_qsu.pp

# Verify the module is active.
if ! semodule -l | grep -q '^qdistro_qsu\b'; then
    echo "[qsu-install] FAIL: qdistro_qsu not listed by semodule -l" >&2
    exit 2
fi

# Apply the .fc label to the already-installed service script so the
# transition target carries qdistro_root_exec_exec_t.
if [ -e /usr/local/lib/qdistro/qdistro_root_exec.py ]; then
    restorecon -v /usr/local/lib/qdistro/qdistro_root_exec.py || true
fi

echo "[qsu-install] OK — qdistro_qsu active"
