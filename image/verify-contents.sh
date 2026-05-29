#!/bin/bash
# verify-contents.sh — STATIC image-content checklist.
#
# Inspect an extracted / mounted qdistro image tree (a root filesystem)
# WITHOUT booting a VM, and assert the things that `verify.sh` can only
# confirm at runtime are at least present on disk. This is the
# "fail-fast before boot" gate: it catches a build that silently dropped
# a binary, a unit, an install root, or a SELinux module long before the
# ~5-min boot-verify cycle.
#
# Pattern: each check prints `OK   <label>: <path>` or `MISS <label>:
# <path>`. Required misses make the script exit nonzero; optional misses
# print WARN and do not fail.
#
# Usage:
#   ./verify-contents.sh <root>
#     <root> is a directory that is the image root filesystem, e.g. a
#     loop-mounted btrfs root, an extracted overlay tree, or the kiwi
#     `root/` overlay (partial — most checks will WARN/MISS there).
#
# Exit codes:
#   0  all required items present
#   1  one or more required items missing
#   2  usage / bad root path (nonexistent, not a directory)
#
# This is intentionally a host-only static inspector. It does not need
# libvirt, ssh, sshpass, or any VM. It is safe to run in CI preflight or
# as the first stage of `qci image`.

set -uo pipefail

PROG=$(basename "$0")

usage() {
    cat <<EOF
Usage: $PROG <image-root-dir>

Statically inspect an extracted/mounted qdistro image root filesystem
(no VM boot). Prints OK/MISS/WARN per check; exits nonzero if any
REQUIRED item is missing, 2 on a bad/nonexistent root path.

Examples:
  $PROG /mnt/qdistro-root
  $PROG /tmp/qdistro-build/extracted
EOF
}

case "${1:-}" in
    -h|--help|"") usage; exit 2 ;;
esac

ROOT=$1
if [ ! -e "$ROOT" ]; then
    printf 'FATAL: image root does not exist: %s\n' "$ROOT" >&2
    exit 2
fi
if [ ! -d "$ROOT" ]; then
    printf 'FATAL: image root is not a directory: %s\n' "$ROOT" >&2
    printf 'hint: mount/extract the raw image first, then pass the mountpoint.\n' >&2
    exit 2
fi
# Normalize (strip trailing slash) for clean path printing.
ROOT=${ROOT%/}

FAIL=0
REQUIRED_TOTAL=0
REQUIRED_OK=0
OPT_TOTAL=0
OPT_OK=0

# check_req <label> <test-expr-as-path> — a path under $ROOT that must exist
check_req() {
    local label=$1 rel=$2 full="$ROOT/${2#/}"
    REQUIRED_TOTAL=$((REQUIRED_TOTAL + 1))
    if [ -e "$full" ]; then
        printf 'OK   %s: %s\n' "$label" "$full"
        REQUIRED_OK=$((REQUIRED_OK + 1))
    else
        printf 'MISS %s: %s\n' "$label" "$full"
        FAIL=1
    fi
}

# check_req_any <label> <rel1> [rel2 ...] — at least one path must exist
check_req_any() {
    local label=$1; shift
    local rel full hit=0 tried=()
    REQUIRED_TOTAL=$((REQUIRED_TOTAL + 1))
    for rel in "$@"; do
        full="$ROOT/${rel#/}"
        tried+=("$full")
        if [ -e "$full" ]; then
            printf 'OK   %s: %s\n' "$label" "$full"
            REQUIRED_OK=$((REQUIRED_OK + 1))
            hit=1
            break
        fi
    done
    if [ "$hit" = 0 ]; then
        printf 'MISS %s: (none of) %s\n' "$label" "${tried[*]}"
        FAIL=1
    fi
}

# check_opt <label> <rel> — optional; MISS => WARN, never fails
check_opt() {
    local label=$1 rel=$2 full="$ROOT/${2#/}"
    OPT_TOTAL=$((OPT_TOTAL + 1))
    if [ -e "$full" ]; then
        printf 'OK   %s: %s\n' "$label" "$full"
        OPT_OK=$((OPT_OK + 1))
    else
        printf 'WARN %s (optional): %s\n' "$label" "$full"
    fi
}

# check_glob_req <label> <glob-under-root> — required; matches >=1 path
check_glob_req() {
    local label=$1 glob=$2
    REQUIRED_TOTAL=$((REQUIRED_TOTAL + 1))
    # shellcheck disable=SC2086
    local matches
    matches=$(compgen -G "$ROOT/${glob#/}" 2>/dev/null | head -5)
    if [ -n "$matches" ]; then
        printf 'OK   %s: %s\n' "$label" "$(echo "$matches" | head -1)"
        REQUIRED_OK=$((REQUIRED_OK + 1))
    else
        printf 'MISS %s: %s\n' "$label" "$ROOT/${glob#/}"
        FAIL=1
    fi
}

# check_glob_opt <label> <glob-under-root> — optional glob
check_glob_opt() {
    local label=$1 glob=$2 matches
    OPT_TOTAL=$((OPT_TOTAL + 1))
    matches=$(compgen -G "$ROOT/${glob#/}" 2>/dev/null | head -5)
    if [ -n "$matches" ]; then
        printf 'OK   %s: %s\n' "$label" "$(echo "$matches" | head -1)"
        OPT_OK=$((OPT_OK + 1))
    else
        printf 'WARN %s (optional): %s\n' "$label" "$ROOT/${glob#/}"
    fi
}

echo "== qdistro static image-content checklist =="
echo "root: $ROOT"
echo

echo "-- branding / identity --"
check_req_any "os-release present"        /etc/os-release /usr/lib/os-release
check_opt     "qdistro-release marker"    /etc/qdistro-release

echo
echo "-- in-place source tree (LLM-modifiability) --"
# /root/qdistro-src/{qdistro,qdwin,qdshell} must survive onto the image.
check_req "qdistro source root"  /root/qdistro-src
check_req "qdistro src"          /root/qdistro-src/qdistro
check_req "qdwin src"            /root/qdistro-src/qdwin
check_req "qdshell src"         /root/qdistro-src/qdshell

echo
echo "-- qdwin / qdshell / qdistro install roots --"
check_req_any "qdwin weston shell" \
    /usr/lib64/weston/qdwin-shell.so /usr/lib/weston/qdwin-shell.so
check_req     "qdshell QML install" /usr/share/quickshell/qdshell
check_opt     "qdwin session launcher" /usr/local/bin/qdwin-session-launcher

echo
echo "-- admin uid assumptions (single-tenant: admin=1000) --"
# Static proxy for "admin uid 1000": passwd entry + home dir. We grep
# the on-disk passwd, not a live `id`, so this stays bootless.
REQUIRED_TOTAL=$((REQUIRED_TOTAL + 1))
if [ -f "$ROOT/etc/passwd" ] && grep -qE '^admin:[^:]*:1000:' "$ROOT/etc/passwd"; then
    printf 'OK   %s: %s\n' "admin uid 1000 in passwd" "$ROOT/etc/passwd"
    REQUIRED_OK=$((REQUIRED_OK + 1))
elif [ -d "$ROOT/home/admin" ]; then
    # Fall back to the home dir if passwd is not in this tree slice.
    printf 'OK   %s: %s\n' "admin home dir (passwd not in tree)" "$ROOT/home/admin"
    REQUIRED_OK=$((REQUIRED_OK + 1))
else
    printf 'MISS %s: %s\n' "admin uid 1000" "$ROOT/etc/passwd or $ROOT/home/admin"
    FAIL=1
fi
check_opt "admin home"             /home/admin

echo
echo "-- systemd units present --"
check_req_any "admin broker unit" \
    /etc/systemd/system/qdistro-admin-broker.service \
    /usr/lib/systemd/system/qdistro-admin-broker.service
check_req_any "dbus-reload unit" \
    /etc/systemd/system/qdistro-dbus-reload.service \
    /usr/lib/systemd/system/qdistro-dbus-reload.service
check_opt "root-exec unit" \
    /usr/lib/systemd/system/qdistro-root-exec.service
check_opt "greetd unit" \
    /usr/lib/systemd/system/greetd.service

echo
echo "-- systemd units enabled (wants/ symlinks) --"
# System-level enable lands as a symlink under */.wants/. User units
# (noctalia-*) are enabled per the config.sh trick by writing symlinks
# under admin's ~/.config/systemd/user/default.target.wants/.
check_glob_opt "system multi-user.wants symlinks" \
    "/etc/systemd/system/multi-user.target.wants/*"
check_glob_opt "greetd enabled (wants symlink)" \
    "/etc/systemd/system/*.wants/greetd.service"
check_glob_opt "noctalia user units enabled (admin default.target.wants)" \
    "/home/admin/.config/systemd/user/default.target.wants/noctalia-*.service"

echo
echo "-- SELinux policy modules / files --"
# Modules ship as compiled .pp under /usr/share/selinux or get loaded
# into the active store under /etc/selinux/targeted/active/modules.
check_glob_req "SELinux config present" "/etc/selinux/config"
check_glob_opt "qdistro SELinux .pp modules" \
    "/usr/share/selinux/*/qdistro_*.pp"
check_glob_opt "qdistro SELinux .pp (packages dir)" \
    "/usr/share/selinux/packages/qdistro_*.pp"
check_glob_opt "SELinux active module store" \
    "/etc/selinux/targeted/active/modules/*/qdistro_*"

echo
echo "-- broker / qsu / browser-bridge binaries & scripts --"
check_req     "broker libexec dir"     /usr/libexec/qdistro
check_glob_req "broker python module"  "/usr/libexec/qdistro/*.py"
check_req     "qsu compiled binary"    /usr/local/bin/qsu
check_opt     "browser-bridge exec stub" /usr/lib/qdistro/browser-bridge
check_opt     "browser-bridge host module" /usr/libexec/qdistro/qdistro_browser_bridge.py
check_opt     "browser-install CLI"    /usr/local/bin/qdistro-browser-install

echo
echo "== summary =="
printf 'required: %d/%d present; optional: %d/%d present\n' \
    "$REQUIRED_OK" "$REQUIRED_TOTAL" "$OPT_OK" "$OPT_TOTAL"
if [ "$FAIL" -ne 0 ]; then
    echo "RESULT: FAIL — one or more required image-content items missing."
    exit 1
fi
echo "RESULT: PASS — all required image-content items present."
exit 0
