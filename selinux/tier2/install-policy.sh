#!/bin/bash
# Build + install the qdistro_tier2 SELinux policy module on a fresh
# clone. Idempotent.
#
# Pre-reqs: checkpolicy (checkmodule) + policycoreutils (semodule_package,
# semodule) + container-selinux (provides container_t and the
# container_domain / svirt_sandbox_domain / mcs_constrained_type
# attributes this module requires).
# Tumbleweed: `zypper install checkpolicy policycoreutils container-selinux`.
#
# Unlike tier1, this module is kernel policy language and does NOT need
# selinux-policy-devel — see the Makefile header for why.
#
# SPDX-License-Identifier: MIT
set -euo pipefail

DIR=$(cd "$(dirname "$0")" && pwd)
cd "$DIR"

if ! command -v checkmodule >/dev/null 2>&1; then
    echo "[tier2-install] FAIL: checkmodule not installed (zypper install checkpolicy)" >&2
    exit 1
fi
if ! command -v semodule >/dev/null 2>&1; then
    echo "[tier2-install] FAIL: semodule not installed (zypper install policycoreutils)" >&2
    exit 1
fi

# Dependency check: qdistro_tier2.te requires container_t /
# container_file_t / container_use_dri_devices from container-selinux. If
# those aren't in the active policy, `semodule -i` fails to resolve the
# typeattributeset / typebounds. We can detect this with `seinfo` when
# available; otherwise we proceed and let semodule report the resolution
# error (the build itself, below, does not need the live policy).
if command -v seinfo >/dev/null 2>&1; then
    if ! seinfo -t 2>/dev/null | grep -qw container_t; then
        echo "[tier2-install] FAIL: container_t not in active policy —" \
             "install container-selinux first" >&2
        exit 3
    fi
fi

# Build .pp from .te via the base toolchain (checkmodule +
# semodule_package). The Makefile encapsulates the exact incantation.
make MODULE=qdistro_tier2

# Idempotent install — semodule -i replaces an existing module.
semodule -i qdistro_tier2.pp

# Verify the module is active.
if ! semodule -l | grep -q '^qdistro_tier2\b'; then
    echo "[tier2-install] FAIL: qdistro_tier2 not listed by semodule -l" >&2
    exit 2
fi

echo "[tier2-install] OK — qdistro_tier2 active"
echo "[tier2-install] NOTE: the domain is inert until spawn-tier2.sh"
echo "[tier2-install]       passes --security-opt label=type:qdistro_tier2_t"
echo "[tier2-install]       (see selinux/tier2/README.md 'Engaging the domain')."
