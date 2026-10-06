#!/bin/bash
# Build + install the qdistro_broker_rules SELinux policy module.
# Idempotent.
#
# Pre-reqs: checkpolicy (checkmodule) + policycoreutils
# (semodule_package, semodule, restorecon). The module requires the
# qdistro_broker module's qdistro_broker_t — install selinux/broker
# first (the bootstrap loop and guest-setup both order it that way).
#
# SPDX-License-Identifier: MIT
set -euo pipefail

DIR=$(cd "$(dirname "$0")" && pwd)
cd "$DIR"

if ! command -v checkmodule >/dev/null 2>&1; then
    echo "[broker-rules-install] FAIL: checkmodule not installed (zypper install checkpolicy)" >&2
    exit 1
fi
if ! command -v semodule >/dev/null 2>&1; then
    echo "[broker-rules-install] FAIL: semodule not installed (zypper install policycoreutils)" >&2
    exit 1
fi

# Build .pp from .te + .fc via the base toolchain directly (no make on
# the VM images; the Makefile is a host convenience only).
checkmodule -M -m -o qdistro_broker_rules.mod qdistro_broker_rules.te
semodule_package -o qdistro_broker_rules.pp -m qdistro_broker_rules.mod -f qdistro_broker_rules.fc

# Idempotent install — semodule -i replaces an existing module and
# merges the packaged file contexts.
semodule -i qdistro_broker_rules.pp

if ! semodule -l | grep -q '^qdistro_broker_rules\b'; then
    echo "[broker-rules-install] FAIL: qdistro_broker_rules not listed by semodule -l" >&2
    exit 2
fi

# Relabel rules.d when it already exists (it is created etc_t at
# install/bootstrap time before this module can ship its context).
if command -v restorecon >/dev/null 2>&1; then
    [ -d /etc/qdistro/rules.d ] && \
        restorecon -R /etc/qdistro/rules.d
fi

echo "[broker-rules-install] OK — qdistro_broker_rules active"
