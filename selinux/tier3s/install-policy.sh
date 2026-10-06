#!/bin/bash
# Build + install the qdistro_tier3s SELinux policy module. Idempotent.
#
# Pre-reqs: checkpolicy (checkmodule) + policycoreutils (semodule_package,
# semodule, restorecon). The module does not depend on container-selinux:
# the tier3s domain is a from-scratch domain, not a container_t sibling,
# and podman launches it with label=disable by contract.
#
# SPDX-License-Identifier: MIT
set -euo pipefail

DIR=$(cd "$(dirname "$0")" && pwd)
cd "$DIR"

if ! command -v checkmodule >/dev/null 2>&1; then
    echo "[tier3s-install] FAIL: checkmodule not installed (zypper install checkpolicy)" >&2
    exit 1
fi
if ! command -v semodule >/dev/null 2>&1; then
    echo "[tier3s-install] FAIL: semodule not installed (zypper install policycoreutils)" >&2
    exit 1
fi

# Build .pp from .te + .fc via the base toolchain directly (no make on
# the VM images; the Makefile is a host convenience only).
checkmodule -M -m -o qdistro_tier3s.mod qdistro_tier3s.te
semodule_package -o qdistro_tier3s.pp -m qdistro_tier3s.mod -f qdistro_tier3s.fc

# Idempotent install — semodule -i replaces an existing module and
# merges the packaged file contexts.
semodule -i qdistro_tier3s.pp

if ! semodule -l | grep -q '^qdistro_tier3s\b'; then
    echo "[tier3s-install] FAIL: qdistro_tier3s not listed by semodule -l" >&2
    exit 2
fi

# Apply the file contexts. The runsc tree exists only after
# tier3s/provision-runsc.sh has run; the /run state dirs are tmpfs and
# may not exist either — every restorecon is conditional, none is
# required for the module to be loaded correctly (the contexts still
# apply at file creation time via the installed file_contexts).
if command -v restorecon >/dev/null 2>&1; then
    [ -d /usr/libexec/qdistro/runsc ] && \
        restorecon -R /usr/libexec/qdistro/runsc
    [ -d /run/qdistro-tier3s-runsc ] && \
        restorecon -R /run/qdistro-tier3s-runsc
    [ -d /run/qdistro-tier3s ] && \
        restorecon -R /run/qdistro-tier3s
    [ -d /run/qdistro-tier3s-ctl ] && \
        restorecon -R /run/qdistro-tier3s-ctl
fi

echo "[tier3s-install] OK — qdistro_tier3s active"
