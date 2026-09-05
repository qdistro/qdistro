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
  $PROG /var/tmp/qdistro-build/extracted
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
# Canonicalise once: every candidate path is compared textually against
# $ROOT below, so $ROOT must be the real directory (not a symlink to it,
# not spelled with trailing slashes).
ROOT="$(realpath -e -- "$ROOT")"

FAIL=0
REQUIRED_TOTAL=0
REQUIRED_OK=0
OPT_TOTAL=0
OPT_OK=0

# resolve_in_image <host-path-under-ROOT> — resolve the path the way the
# BOOTED IMAGE would, i.e. with $ROOT as `/`: walk component by component
# from $ROOT; a symlink component (ancestor or final) is followed with an
# absolute target rebased onto $ROOT and a relative target spliced in place;
# `..` never climbs above $ROOT; at most 40 symlink hops. Prints the fully
# resolved host path (which by construction contains no symlink and lies
# under $ROOT), or fails. Nothing here consults the host's own tree: a bare
# `-e` followed an absolute link in the HOST namespace (`usr/bin/qdlocker ->
# /bin/sh` passed with no bin/sh in the tree, round-2 review) and an
# ancestor that is a symlink to a host directory made every file below it
# a host lookup (round-3 review). This resolver has neither hole.
resolve_in_image() {
    local rest="${1#"$ROOT"}" cur="$ROOT" comp next target hops=0
    rest="${rest#/}"
    while [ -n "$rest" ]; do
        comp="${rest%%/*}"
        if [ "$rest" = "$comp" ]; then rest=""; else rest="${rest#*/}"; fi
        case "$comp" in
            ""|.) continue ;;
            # Linux clamps `..` at `/`; with $ROOT as `/` so do we (round-4
            # review: failing here made `usr/bin -> ../../` a false MISS).
            ..) [ "$cur" = "$ROOT" ] || cur="${cur%/*}"; continue ;;
        esac
        next="$cur/$comp"
        if [ -L "$next" ]; then
            hops=$((hops + 1)); [ "$hops" -le 40 ] || return 1
            target="$(readlink "$next")"
            case "$target" in
                /*) cur="$ROOT"; target="${target#/}" ;;
            esac
            rest="$target${rest:+/$rest}"
            continue
        fi
        [ -e "$next" ] || return 1
        cur="$next"
    done
    printf '%s\n' "$cur"
}

# in_image <path> — the artifact exists IN THE IMAGE (see resolve_in_image).
in_image() {
    local r
    r="$(resolve_in_image "$1")" || return 1
    [ -e "$r" ]
}

# entry_in_image <path> — resolve the PARENT with image semantics and print
# the host path of the directory entry itself (which may be a symlink, or
# absent). For checks about the entry as such: `-L`, readlink, absence.
entry_in_image() {
    local parent
    parent="$(resolve_in_image "$(dirname "$1")")" || return 1
    printf '%s/%s\n' "$parent" "$(basename "$1")"
}

# file_in_image <path> — print the host path of a regular file resolved with
# image semantics, for reading its CONTENT (never read "$ROOT/x" directly:
# an ancestor or the file itself may be a symlink into the host).
file_in_image() {
    local r
    r="$(resolve_in_image "$1")" || return 1
    [ -f "$r" ] || return 1
    printf '%s\n' "$r"
}

# check_req <label> <test-expr-as-path> — a path under $ROOT that must exist
# in the image (see in_image for what a symlink has to satisfy).
check_req() {
    local label=$1 rel=$2 full="$ROOT/${2#/}"
    REQUIRED_TOTAL=$((REQUIRED_TOTAL + 1))
    if in_image "$full"; then
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
        if in_image "$full"; then
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
    if in_image "$full"; then
        printf 'OK   %s: %s\n' "$label" "$full"
        OPT_OK=$((OPT_OK + 1))
    else
        printf 'WARN %s (optional): %s\n' "$label" "$full"
    fi
}

# glob_in_image <glob-under-root> — expand a glob with the booted image's
# path semantics and print the first IMAGE path (under $ROOT) that resolves
# to an existing artifact, or nothing. `compgen -G "$ROOT/pattern"` alone
# walks the pattern's prefixes in the HOST namespace, so a directory that
# is an absolute symlink inside the image hid every match below it (round-5
# review: `/usr/libexec/qdistro -> /inside-libexec` gave a false MISS).
# Here each component is expanded against the resolver's host directory
# for the image path so far, and every match is re-resolved as an image
# path before the walk continues; enumeration is capped per level.
glob_in_image() {
    local pattern="${1#/}" comp rest cands=("") next=() c host names n
    rest="$pattern"
    while [ -n "$rest" ]; do
        comp="${rest%%/*}"
        if [ "$rest" = "$comp" ]; then rest=""; else rest="${rest#*/}"; fi
        [ -n "$comp" ] || continue
        next=()
        for c in "${cands[@]}"; do
            case "$comp" in
                *[*?[]*)
                    # The image directory so far, as a real host dir.
                    host="$(resolve_in_image "$ROOT/$c")" || continue
                    [ -d "$host" ] || continue
                    n=0
                    while IFS= read -r names; do
                        [ -n "$names" ] || continue
                        n=$((n + 1)); [ "$n" -le 200 ] || break
                        next+=("${c:+$c/}${names##*/}")
                    done < <(compgen -G "$host/$comp" 2>/dev/null)
                    ;;
                *)  next+=("${c:+$c/}$comp") ;;
            esac
        done
        [ "${#next[@]}" -gt 0 ] || return 1
        cands=("${next[@]}")
    done
    for c in "${cands[@]}"; do
        if in_image "$ROOT/$c"; then printf '%s\n' "$ROOT/$c"; return 0; fi
    done
    return 1
}

# check_glob_req <label> <glob-under-root> — required; matches >=1 path
check_glob_req() {
    local label=$1 glob=$2
    REQUIRED_TOTAL=$((REQUIRED_TOTAL + 1))
    # shellcheck disable=SC2086
    local hit
    hit="$(glob_in_image "$glob")" || hit=""
    if [ -n "$hit" ]; then
        printf 'OK   %s: %s\n' "$label" "$hit"
        REQUIRED_OK=$((REQUIRED_OK + 1))
    else
        printf 'MISS %s: %s (no match resolves in the image)\n' "$label" "$ROOT/${glob#/}"
        FAIL=1
    fi
}

# check_glob_opt <label> <glob-under-root> — optional glob
check_glob_opt() {
    local label=$1 glob=$2 hit
    OPT_TOTAL=$((OPT_TOTAL + 1))
    hit="$(glob_in_image "$glob")" || hit=""
    if [ -n "$hit" ]; then
        printf 'OK   %s: %s\n' "$label" "$hit"
        OPT_OK=$((OPT_OK + 1))
    else
        printf 'WARN %s (optional): %s\n' "$label" "$ROOT/${glob#/}"
    fi
}

# check_absent <label> <rel> — a path that must NOT exist (a wiring the image
# deliberately leaves out); present => FAIL
check_absent() {
    local label=$1 rel=$2 full="$ROOT/${2#/}" entry
    REQUIRED_TOTAL=$((REQUIRED_TOTAL + 1))
    # The parent is resolved with image semantics; a parent that does not
    # exist in the image means the entry is absent too.
    entry="$(entry_in_image "$full")" || entry=""
    if [ -z "$entry" ] || { [ ! -e "$entry" ] && [ ! -L "$entry" ]; }; then
        printf 'OK   %s: absent as required: %s\n' "$label" "$full"
        REQUIRED_OK=$((REQUIRED_OK + 1))
    else
        printf 'FAIL %s: must be absent but exists: %s\n' "$label" "$full"
        FAIL=1
    fi
}

# check_link <label> <rel> — a systemd wants-link (or any symlink) that must
# exist AND whose target must exist inside the image. systemd writes these
# ABSOLUTE (`-> /etc/systemd/user/foo.service`), so read from the host they
# dangle; the target is therefore resolved under $ROOT. A relative target is
# resolved against the link's directory. A missing link, a non-link, or a
# target that is not in the image all FAIL.
check_link() {
    local label=$1 rel=$2 full="$ROOT/${2#/}" entry target
    REQUIRED_TOTAL=$((REQUIRED_TOTAL + 1))
    entry="$(entry_in_image "$full")" || entry=""
    if [ -z "$entry" ] || [ ! -L "$entry" ]; then
        printf 'MISS %s: not a symlink: %s\n' "$label" "$full"
        FAIL=1
        return
    fi
    target="$(readlink "$entry")"
    if in_image "$full"; then
        printf 'OK   %s: %s -> %s\n' "$label" "$full" "$target"
        REQUIRED_OK=$((REQUIRED_OK + 1))
    else
        printf 'MISS %s: %s -> %s (target absent in image or escapes it)\n' "$label" "$full" "$target"
        FAIL=1
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
# greetd is enabled to exec /usr/bin/qdgreeter (deploy/greetd-config.toml).
# A missing greeter binary means the primary login path boots to a
# non-existent command (finding #19) — REQUIRED static gate.
check_req     "qdgreeter binary" /usr/bin/qdgreeter

echo
echo "-- admin uid assumptions (single-tenant: admin=1000) --"
# Static proxy for "admin uid 1000": passwd entry + home dir. We grep
# the on-disk passwd, not a live `id`, so this stays bootless.
REQUIRED_TOTAL=$((REQUIRED_TOTAL + 1))
passwd_file="$(file_in_image "$ROOT/etc/passwd" 2>/dev/null || true)"
if [ -n "$passwd_file" ] && grep -qE '^admin:[^:]*:1000:' "$passwd_file"; then
    printf 'OK   %s: %s\n' "admin uid 1000 in passwd" "$ROOT/etc/passwd"
    REQUIRED_OK=$((REQUIRED_OK + 1))
elif in_image "$ROOT/etc"; then
    # /etc is in this tree, so passwd should have been found as a regular
    # in-image file with the admin line; falling back to the home dir here
    # would let a passwd that is a symlink into the host pass (round-4).
    printf 'MISS %s: %s (present /etc but no in-image passwd with admin:1000)\n' "admin uid 1000" "$ROOT/etc/passwd"
    FAIL=1
elif in_image "$ROOT/home/admin"; then
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
check_opt "greetd unit" \
    /usr/lib/systemd/system/greetd.service

echo
echo "-- production session units (greeter boot path) --"
# The greeter (greetd -> qdgreeter -> qdwin-session-launcher) starts
# qdwin-session.target, which Wants= qdshell.service + qdlocker.service
# and Requires= qdwin-compositor.service. These unit FILES must be
# installed in admin's user systemd dir or the greeter authenticates and
# then hands greetd a target that does not exist (findings #15, #16).
check_req "qdwin-session.target (admin user unit)" \
    /home/admin/.config/systemd/user/qdwin-session.target
check_req "qdwin-compositor.service (admin user unit)" \
    /home/admin/.config/systemd/user/qdwin-compositor.service
check_req "qdshell.service (admin user unit)" \
    /home/admin/.config/systemd/user/qdshell.service
check_req "qdlocker.service (admin user unit)" \
    /home/admin/.config/systemd/user/qdlocker.service

echo
echo "-- systemd units enabled (wants/ symlinks) --"
# System-level enable lands as a symlink under */.wants/.
check_glob_opt "system multi-user.wants symlinks" \
    "/etc/systemd/system/multi-user.target.wants/*"
# greetd's only [Install] is Alias=display-manager.service, so `enable`
# creates exactly that alias link and graphical.target's built-in
# Wants=display-manager.service starts it. Required: without the alias the
# image boots to no login at all.
check_link "greetd enabled (display-manager alias)" \
    /etc/systemd/system/display-manager.service
# qdshell + qdlocker are wired into qdwin-session.target.wants/ so the
# greeter-started target pulls them in (findings #15, #16). The target
# itself is started transiently by the launcher, not enabled under
# default.target.
check_link "qdshell wired into qdwin-session.target.wants" \
    /home/admin/.config/systemd/user/qdwin-session.target.wants/qdshell.service
check_link "qdlocker wired into qdwin-session.target.wants" \
    /home/admin/.config/systemd/user/qdwin-session.target.wants/qdlocker.service

echo
echo "-- SELinux policy modules / files --"
# Modules ship as compiled .pp under /usr/share/selinux or get loaded
# into the active store under /etc/selinux/targeted/active/modules.
check_glob_req "SELinux config present" "/etc/selinux/config"
check_glob_opt "qdistro SELinux .pp modules" \
    "/usr/share/selinux/*/qdistro_*.pp"
check_glob_opt "qdistro SELinux .pp (packages dir)" \
    "/usr/share/selinux/packages/qdistro_*.pp"
# The four qdistro policy modules must be IN the module store: config.sh's
# policy installs are fatal on failure, but the policy installer SKIPs with
# exit 0 when selinux-policy-devel is absent, so an image built without it
# would ship every qdistro service unconfined and still be green (Phase B
# round-2 review). SELINUX= is permissive today (Phase C); this is about
# completeness, not enforcement.
check_req "[selinux] qdistro_broker module"          /etc/selinux/targeted/active/modules/400/qdistro_broker
check_req "[selinux] qdistro_pwd module"             /etc/selinux/targeted/active/modules/400/qdistro_pwd
check_req "[selinux] qdistro_session_manager module" /etc/selinux/targeted/active/modules/400/qdistro_session_manager
check_req "[selinux] qdistro_tier1 module"           /etc/selinux/targeted/active/modules/400/qdistro_tier1
check_glob_opt "SELinux active module store" \
    "/etc/selinux/targeted/active/modules/*/qdistro_*"

echo
echo "-- broker / qsu / browser-bridge binaries & scripts --"
check_req     "broker libexec dir"     /usr/libexec/qdistro
check_glob_req "broker python module"  "/usr/libexec/qdistro/*.py"
check_req     "qsu compiled binary"    /usr/local/bin/qsu
check_opt     "browser-bridge exec stub" /usr/lib/qdistro/browser-bridge
check_opt     "browser-bridge host module" /usr/libexec/qdistro/qdistro_browser_bridge.py
check_opt     "browser-allowlist shared module" /usr/libexec/qdistro/qdistro_browser_allowlist.py
check_opt     "browser-install CLI"    /usr/local/bin/qdistro-browser-install
# The user-relay must land in the SAME dir as the broker modules: its
# Firefox-containers cross-uid gate imports qdistro_admin_rules from its own
# directory. Installed anywhere else the gate fails closed forever (F4).
check_req     "user-relay module"      /usr/libexec/qdistro/qdistro_user_relay.py
check_req     "user-relay rules dep"   /usr/libexec/qdistro/qdistro_admin_rules.py
check_req     "user-relay unit template" \
    /etc/systemd/system/qdistro-user-relay@.service

echo
echo "-- utility app desktop integration assets --"
# qterminator / qfileman ship .desktop + metainfo + icons in-tree; the
# bootstrap install_app_desktop_assets step lands them under /usr/share so
# the apps are discoverable in the launcher / AppStream / icon theme
# (finding #21). Optional: an image that does not include these utility
# apps legitimately omits them, but when an app's /usr/bin entry exists its
# assets should too.
if in_image "$ROOT/usr/bin/qterminator"; then
    check_req "qterminator .desktop"  /usr/share/applications/qterminator.desktop
    check_req "qterminator metainfo"  /usr/share/metainfo/qterminator.metainfo.xml
    check_glob_req "qterminator icon" "/usr/share/icons/hicolor/*/apps/qterminator.*"
else
    check_opt "qterminator .desktop"  /usr/share/applications/qterminator.desktop
    check_opt "qterminator metainfo"  /usr/share/metainfo/qterminator.metainfo.xml
fi
if in_image "$ROOT/usr/bin/qfileman"; then
    check_req "qfileman .desktop"  /usr/share/applications/qfileman.desktop
    check_req "qfileman metainfo"  /usr/share/metainfo/qfileman.metainfo.xml
    check_glob_req "qfileman icon" "/usr/share/icons/hicolor/*/apps/qfileman.*"
else
    check_opt "qfileman .desktop"  /usr/share/applications/qfileman.desktop
    check_opt "qfileman metainfo"  /usr/share/metainfo/qfileman.metainfo.xml
fi

echo
echo "-- installer chain: one row per step (todo/iso/14 Phase B) --"
# Each image/config.sh chain step must have dropped the artifacts it exists
# for. Under the old fail-open loop an installer could die half-way and the
# build still went green; these rows are the static half of "a green build
# means a complete image" (the fatal chain is the other half). Keep in the
# chain's order. A step's row names its unit(s), policy and main binary/module.
check_req "[broker] unit"              /etc/systemd/system/qdistro-admin-broker.service
check_req "[broker] dbus-reload unit"  /etc/systemd/system/qdistro-dbus-reload.service
check_req "[broker] bus policy"        /etc/dbus-1/system.d/org.qdistro.AdminBroker1.conf
check_req "[broker] daemon module"     /usr/libexec/qdistro/qdistro_admin_broker.py
check_link "[broker] enabled"          /etc/systemd/system/multi-user.target.wants/qdistro-admin-broker.service
check_link "[broker] dbus-reload enabled" /etc/systemd/system/multi-user.target.wants/qdistro-dbus-reload.service
check_link "[broker] dbus-reload wanted by broker" /etc/systemd/system/qdistro-admin-broker.service.wants/qdistro-dbus-reload.service
check_req "[user-relay] bus policy"    /etc/dbus-1/system.d/org.qdistro.UserRelay.conf
check_req "[session-manager] unit"     /etc/systemd/system/qdistro-session-manager.service
check_req "[session-manager] bus policy" /etc/dbus-1/system.d/org.qdistro.SessionManager1.conf
check_req "[session-manager] daemon module" /usr/libexec/qdistro/qdistro_session_manager.py
check_req "[session-manager] egress module"  /usr/libexec/qdistro/qdistro_silo_egress.py
check_req "[session-manager] qdshell-session template" /usr/lib/systemd/system/qdshell-session@.service
check_req "[session-manager] tier2 silo unit" /etc/systemd/system/qdistro-tier2-silo@.service
check_req "[session-manager] podapp unit" /etc/systemd/system/qdistro-podapp@.service
check_link "[session-manager] silo-launch CLI" /usr/local/bin/qdistro-silo-launch
check_link "[session-manager] work silo link" /etc/systemd/system/qdshell-session-work@.service
check_link "[session-manager] enabled" /etc/systemd/system/multi-user.target.wants/qdistro-session-manager.service
check_req "[polkit-agent] user unit"   /etc/systemd/user/qdistro-polkit-agent.service
check_link "[polkit-agent] session wants link" /etc/systemd/user/qdwin-session.target.wants/qdistro-polkit-agent.service
check_req "[polkit-agent] module"      /usr/libexec/qdistro/qdistro_polkit_agent.py
check_req "[polkit-agent] prompt helper" /usr/local/bin/qdistro-polkit-prompt
check_req "[pwd] unit"                 /etc/systemd/system/qdistro-pwd.service
check_req "[pwd] bus policy"           /etc/dbus-1/system.d/org.qdistro.Pwd1.conf
check_req "[pwd] daemon module"        /usr/libexec/qdistro/qdistro_pwd_daemon.py
check_req "[pwd] polkit action"        /usr/share/polkit-1/actions/org.qdistro.pwd.policy
check_req "[pwd] portal-keys unlock user unit" /etc/systemd/user/qdistro-portal-keys-unlock.service
check_link "[pwd] portal-keys unlock wants link" /etc/systemd/user/qdwin-session.target.wants/qdistro-portal-keys-unlock.service
check_link "[pwd] enabled"             /etc/systemd/system/multi-user.target.wants/qdistro-pwd.service
check_req "[pwd] PortalSecret portal"  /usr/share/xdg-desktop-portal/portals/org.qdistro.PortalSecret.portal
check_req "[pwd] CLI"                  /usr/local/bin/qdistro-pwd-get
check_req "[qsu] socket unit"          /etc/systemd/system/qdistro-root-exec.socket
check_req "[qsu] service unit"         /etc/systemd/system/qdistro-root-exec.service
check_req "[qsu] root-exec module"     /usr/local/lib/qdistro/qdistro_root_exec.py
check_link "[qsu] socket enabled"      /etc/systemd/system/sockets.target.wants/qdistro-root-exec.socket
check_req "[media] socket unit"        /etc/systemd/system/qdistro-media-exec.socket
check_req "[media] service unit"       /etc/systemd/system/qdistro-media-exec.service
check_req "[media] exec module"        /usr/local/lib/qdistro/qdistro_media_exec.py
check_link "[media] socket enabled"    /etc/systemd/system/sockets.target.wants/qdistro-media-exec.socket
check_req "[multimachine] broker module" /usr/local/lib/qdistro/multimachine/mm_broker.py
check_req "[multimachine] broker CLI"  /usr/local/bin/qdistro-mm-broker
check_req "[multimachine] session launcher" /usr/local/bin/qdistro-mm-session-launcher
check_req "[multimachine] rdp client wrapper"  /usr/local/bin/qdistro-mm-rdp-client-wrapper
check_req "[browser-bridge] host module" /usr/libexec/qdistro/qdistro_browser_bridge.py
check_req "[browser-bridge] downloads user unit" /etc/systemd/user/qdistro-downloads.service
# `systemctl --global enable` of the four browser daemons writes two links
# each (WantedBy=default.target and qdwin-session.target): all eight.
check_link "[browser-bridge] downloads: session wants link" /etc/systemd/user/qdwin-session.target.wants/qdistro-downloads.service
check_link "[browser-bridge] downloads: default wants link" /etc/systemd/user/default.target.wants/qdistro-downloads.service
check_link "[browser-bridge] mpris: session wants link"     /etc/systemd/user/qdwin-session.target.wants/qdistro-mpris.service
check_link "[browser-bridge] mpris: default wants link"     /etc/systemd/user/default.target.wants/qdistro-mpris.service
check_link "[browser-bridge] notifications: session wants link" /etc/systemd/user/qdwin-session.target.wants/qdistro-notifications.service
check_link "[browser-bridge] notifications: default wants link" /etc/systemd/user/default.target.wants/qdistro-notifications.service
check_link "[browser-bridge] compositor: session wants link" /etc/systemd/user/qdwin-session.target.wants/qdistro-compositor.service
check_link "[browser-bridge] compositor: default wants link" /etc/systemd/user/default.target.wants/qdistro-compositor.service
check_req "[portal-backend] backend module" /usr/lib/qdistro/daemons/qdistro_portal_backend.py
check_req "[portal-backend] user unit"  /etc/systemd/user/qdistro-portal-backend.service
check_req "[portal-backend] portal"     /usr/share/xdg-desktop-portal/portals/qdistro.portal
check_req "[portal-backend] bus service" /usr/share/dbus-1/services/org.freedesktop.impl.portal.qdistro.service
check_req "[phone] unit"               /etc/systemd/system/qdistro-phone.service
check_req "[phone] daemon module"      /usr/libexec/qdistro/qdistro_phone_daemon.py
check_req "[print-proxy] unit"         /etc/systemd/system/qdistro-print-proxy.service
check_req "[print-proxy] proxy binary" /usr/local/bin/qdistro-print-proxy
check_req "[print-proxy] polkit action" /usr/share/polkit-1/actions/org.qdistro.print.policy
check_req "[print-proxy] VM template"  /usr/share/qdistro/print-vm/domain-template.xml
check_link "[print-proxy] enabled"     /etc/systemd/system/multi-user.target.wants/qdistro-print-proxy.service
# Installed only when a source manifest exists and /etc has none yet.
check_opt "[print-proxy] manifest"     /etc/qdistro/printvm-manifest.json
check_req "[snapshots] backup unit"    /etc/systemd/system/qdistro-backup.service
check_req "[snapshots] backup timer"   /etc/systemd/system/qdistro-backup.timer
check_req "[snapshots] service module" /usr/libexec/qdistro/qdistro_backup_service.py
check_req "[snapshots] CLI"            /usr/local/bin/qdistro-backup
check_req "[snapshots] verify unit"    /etc/systemd/system/qdistro-backup-verify.service
check_req "[snapshots] verify timer"   /etc/systemd/system/qdistro-backup-verify.timer
check_req "[qdwin-session] ydotoold unit"        /home/admin/.config/systemd/user/ydotoold.service
# Recall is cut from v1 and deliberately not in the chain: none of its
# artefacts may ship.
check_absent "[recall] timer not shipped"  /etc/systemd/system/qdistro-recall@.timer
check_absent "[recall] unit not shipped"   /etc/systemd/system/qdistro-recall@.service
check_absent "[recall] daemon not shipped" /usr/libexec/qdistro/qdistro_recall_daemon.py
check_absent "[recall] CLI not shipped"    /usr/local/bin/qdistro-recall
# qdwin session installer (called directly by config.sh, same contract).
check_req "[qdwin-session] compositor user unit" /home/admin/.config/systemd/user/qdwin-compositor.service
check_req "[qdwin-session] admin linger marker"  /var/lib/systemd/linger/admin
check_link "[qdwin-session] ydotoold wired"      /home/admin/.config/systemd/user/default.target.wants/ydotoold.service
# The greeter starts the session target; default.target must not (race for
# wayland-1). QDWIN_SESSION_AUTOSTART=0 in config.sh is what guarantees it.
check_absent "[qdwin-session] target NOT auto-started" /home/admin/.config/systemd/user/default.target.wants/qdwin-session.target
check_req "[qdwin-session] greeter launcher"     /usr/local/bin/qdwin-session-launcher
# qdlocker: the binary itself, not just its unit and wants link (Phase B item 3).
check_req "[qdlocker] binary"          /usr/bin/qdlocker

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
