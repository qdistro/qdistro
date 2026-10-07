#!/usr/bin/env bash
# packaging/env.sh — default download sources and build settings for the
# qdistro packaging subtree. Source this file before using the values.
#
# Precedence:  environment  >  packaging/env.local.sh  >  defaults here.
# env.local.sh (gitignored, optional) is applied BEFORE the defaults so that
# dependent defaults (e.g. QDISTRO_WORK deriving from QDISTRO_BUILD_TMP,
# QDISTRO_TARGET_RPM_REPO_URL deriving from QDISTRO_RPM_REPO_URL) compute
# from the resolved value — but an explicitly exported variable always wins.
#
# Nothing here is pinned to a Tumbleweed snapshot: the defaults float on
# Tumbleweed proper. To reproduce an exact package set, point the repo URLs
# at a local mirror or a history/ snapshot via env.local.sh.
#
# The kiwi image pipeline has its own pin mechanism (repo-root
# snapshot.conf, overridable wholesale via QDISTRO_TEST_SUBSTRATE pointing
# at an alternate manifest). This file intentionally does not consume it.

_packaging_vars=(
    QDISTRO_TW_OSS_URL QDISTRO_TW_NONOSS_URL
    QDISTRO_TARGET_OSS_URL QDISTRO_TARGET_NONOSS_URL
    QDISTRO_RPM_REPO_URL QDISTRO_TARGET_RPM_REPO_URL QDISTRO_RPM_KEY_FP
    QDISTRO_RPM_BUILDER QDISTRO_ISO_TOOL
    AGAMA_STOCK_ISO AGAMA_STOCK_ISO_URL AGAMA_STOCK_ISO_SHA256
    QDISTRO_ADMIN_PASSWORD QDISTRO_ROOT_PASSWORD
    QDISTRO_SNAPSHOT_LABEL QDISTRO_VERSION
    QDISTRO_SOURCE_SHA QDISTRO_SOURCE_STATE
    QDISTRO_BUILD_TMP QDISTRO_VM_NAME QDISTRO_WORK
)

# Snapshot env-set values, source env.local.sh, restore: local fills only
# variables the environment left unset.
for _v in "${_packaging_vars[@]}"; do
    if [[ -v $_v ]]; then declare "_packaging_saved_$_v=${!_v}"; fi
done
# shellcheck source=/dev/null
if [ -f "$(dirname "${BASH_SOURCE[0]}")/env.local.sh" ]; then
    . "$(dirname "${BASH_SOURCE[0]}")/env.local.sh"
fi
for _v in "${_packaging_vars[@]}"; do
    declare -n _s="_packaging_saved_$_v"
    if [[ -v _s ]]; then
        printf -v "$_v" '%s' "$_s"
        unset "_packaging_saved_$_v"
    fi
    unset -n _s
done
unset _packaging_vars _v

# --- upstream Tumbleweed repositories used at INSTALL time (Agama) --------
: "${QDISTRO_TW_OSS_URL:=https://download.opensuse.org/tumbleweed/repo/oss/}"
: "${QDISTRO_TW_NONOSS_URL:=https://download.opensuse.org/tumbleweed/repo/non-oss/}"

# --- repositories written into the INSTALLED system -----------------------
# These land in /etc/zypp/repos.d/ on the target and default to upstream
# Tumbleweed INDEPENDENTLY of the install-time URLs — a temporary install
# mirror or snapshot must not silently become the system's update source.
# Set these explicitly to persist a mirror onto installed systems.
: "${QDISTRO_TARGET_OSS_URL:=https://download.opensuse.org/tumbleweed/repo/oss/}"
: "${QDISTRO_TARGET_NONOSS_URL:=https://download.opensuse.org/tumbleweed/repo/non-oss/}"

# --- qdistro RPM repository -----------------------------------------------
# URL the installer pulls qdistro-* packages from, and the repo file left on
# the target. http://10.0.2.2:8877/ reaches a host-side server from a
# slirp/user-net guest; override for real deployments.
: "${QDISTRO_RPM_REPO_URL:=http://10.0.2.2:8877/}"
: "${QDISTRO_TARGET_RPM_REPO_URL:=$QDISTRO_RPM_REPO_URL}"
# RPM signing key fingerprint expected by Agama's gpgFingerprints policy.
: "${QDISTRO_RPM_KEY_FP:=36E477E3A8743BD2898FC3CA73BC8D22961ED8CA}"

# --- container images ------------------------------------------------------
: "${QDISTRO_RPM_BUILDER:=localhost/qdistro-rpm-builder:latest}"
: "${QDISTRO_ISO_TOOL:=localhost/qdistro-iso-tool:latest}"

# --- stock Agama ISO (input to build-custom-iso.sh) ------------------------
# Local file wins when set; otherwise the script downloads AGAMA_STOCK_ISO_URL.
: "${AGAMA_STOCK_ISO:=}"
: "${AGAMA_STOCK_ISO_URL:=https://download.opensuse.org/repositories/systemsmanagement:/Agama:/Devel/images/iso/agama-installer.x86_64-openSUSE.iso}"
# Optional authenticity check of the stock ISO before repacking (sha256).
: "${AGAMA_STOCK_ISO_SHA256:=}"

# --- test-install credentials (TEST-ONLY) ---------------------------------
# Shared prototype credentials, same public test password as image/'s dev
# profile. Override for any install you care about; never ship defaults.
: "${QDISTRO_ADMIN_PASSWORD:=qdistro}"
: "${QDISTRO_ROOT_PASSWORD:=qdistro}"

# --- provenance stamps -----------------------------------------------------
# SNAPSHOT is written to /etc/qdistro/release only when set (the image-side
# release grammar requires 8 digits — leave empty for floating installs).
: "${QDISTRO_SNAPSHOT_LABEL:=}"
: "${QDISTRO_VERSION:=0.1.0}"

# --- source identity for /etc/qdistro/release -------------------------------
# The SOURCE grammar (image/lib/release-stamp.sh) requires
#   SOURCE qdistro <40-hex> clean|DIRTY diff-sha256=<16hex> untracked=<n>
# Defaults resolve the enclosing checkout; override when installing RPMs
# built from a different tree.
# Normalize env.sh's own directory first: callers source it as
# "packaging/agama/../env.sh", and resolving ".." against the raw dirname
# would climb one level too far (into the enclosing checkout).
_env_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
_packaging_repo_root=$(cd "$_env_dir/.." && pwd)
if _head=$(git -C "$_packaging_repo_root" rev-parse --verify --quiet 'HEAD^{commit}' 2>/dev/null); then
    : "${QDISTRO_SOURCE_SHA:=$_head}"
    if [[ ! -v QDISTRO_SOURCE_STATE ]]; then
        _status=$(git -C "$_packaging_repo_root" status --porcelain 2>/dev/null || true)
        if [ -n "$_status" ]; then
            _untracked=$(printf '%s\n' "$_status" | grep -c '^??' || true)
            _dsha=$(git -C "$_packaging_repo_root" diff HEAD 2>/dev/null | sha256sum | cut -c1-16)
            QDISTRO_SOURCE_STATE="DIRTY diff-sha256=$_dsha untracked=$_untracked"
        else
            QDISTRO_SOURCE_STATE=clean
        fi
    fi
fi
: "${QDISTRO_SOURCE_SHA:=0000000000000000000000000000000000000000}"
: "${QDISTRO_SOURCE_STATE:=clean}"
unset _env_dir _packaging_repo_root _head _status _untracked _dsha

# --- scratch space ----------------------------------------------------------
# ISO surgery needs ~15G of scratch (ext4 rootfs + squashfs repack) — do not
# point this at a small /tmp tmpfs.
: "${QDISTRO_BUILD_TMP:=${HOME:?}/.cache/qdistro-packaging}"

# --- test VM definition (agama/vm/install-test.xml.in) ----------------------
# Working dir holding the qcow2 target disk, OEMDRV image, and the custom ISO.
: "${QDISTRO_VM_NAME:=agamatest}"
: "${QDISTRO_WORK:=${QDISTRO_BUILD_TMP}/agama-work}"

# --- cloud / test substrate -------------------------------------------------
# The kiwi/qci VM lanes pick their cloud qcow2 from repo-root snapshot.conf,
# overridable wholesale via QDISTRO_TEST_SUBSTRATE=<path to alternate
# manifest> (consumed by scripts/vm/lib/test-substrate.sh). No packaging-side
# knob is needed — set that variable when booting a different cloud image.

return 0 2>/dev/null || true   # sourcing must not propagate a nonzero status
