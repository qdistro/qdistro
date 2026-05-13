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
