#!/bin/bash
# Build + install the qdistro_tier1 SELinux policy module on a fresh
# clone. Idempotent.
#
# Pre-reqs: selinux-policy-devel + checkpolicy + policycoreutils.
# Tumbleweed: `zypper install selinux-policy-devel checkpolicy policycoreutils`.
# fresh-vm-bootstrap.sh's install-deps step adds these once Tier-1 is
# wired into the deps.list.
#
# This script lives next to the .te/.if/.fc; called from
# fresh-vm-bootstrap.sh once the policy is ready (today not in the
# bootstrap path — see spec/30 §"Phase plan" step 1).
#
# SPDX-License-Identifier: MIT
set -euo pipefail

DIR=$(cd "$(dirname "$0")" && pwd)
cd "$DIR"

if [ ! -d /usr/share/selinux/devel ]; then
    echo "[tier1-install] SKIP: /usr/share/selinux/devel missing" \
        "(install selinux-policy-devel)"
    exit 0
fi

if ! command -v semodule >/dev/null 2>&1; then
    echo "[tier1-install] FAIL: semodule not installed" >&2
    exit 1
fi

# Dependency: qdistro_tier1.te calls qdistro_broker_dbus_chat() and
# also gen_requires qdistro_broker_t directly, so the broker module
# must be present in the active policy store before tier1 loads —
# otherwise semodule fails with `Failed to resolve typeattributeset
# statement at ...cil:59` / `Failed to resolve AST`. Install the
# broker first when its installer is present and the module is not
# already active. Idempotent re-run is safe (the broker installer is
# itself idempotent, and it transitively pulls in qdistro_pwd).
if ! semodule -l 2>/dev/null | grep -q '^qdistro_broker\b'; then
    BROKER_INSTALLER="$DIR/../broker/install-policy.sh"
    if [ -x "$BROKER_INSTALLER" ]; then
        echo "[tier1-install] qdistro_broker not loaded — installing it first"
        (cd "$(dirname "$BROKER_INSTALLER")" && bash install-policy.sh) \
            || { echo "[tier1-install] FAIL: prereq qdistro_broker install failed" >&2; exit 3; }
    fi
fi

# Build .pp from .te/.if/.fc. The system's devel Makefile handles
# the checkmodule + semodule_package incantation.
make MODULE=qdistro_tier1

# Idempotent install — semodule -i replaces an existing module.
semodule -i qdistro_tier1.pp

# Verify the module is active.
if ! semodule -l | grep -q '^qdistro_tier1\b'; then
    echo "[tier1-install] FAIL: qdistro_tier1 not listed by semodule -l" >&2
    exit 2
fi

echo "[tier1-install] OK — qdistro_tier1 active"
