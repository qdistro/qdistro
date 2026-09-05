#!/bin/bash
# harden-compositor-vt.sh — keep the compositor's VT exclusively the
# compositor's.
#
# Called by scripts/install/qdistro-bootstrap.sh (configure_greetd) and by
# image/config.sh, so the bootstrap path and the packaged-image path get the
# same guarantee. Idempotent; safe to re-run.
#
# WHY THIS EXISTS
# ---------------
# The security property is: **the console keyboard on the compositor's VT
# stays K_OFF for the whole greeter/session/locked-session lifetime.** It is
# what stops keystrokes typed at a LOCKED screen from falling through to the
# kernel console layer, where openSUSE's xkb-converted keymap turns ordinary
# chords into VT switches (`keycode 125 = Alt`, `alt keycode 105 =
# Decr_Console` in /usr/share/kbd/keymaps/xkb/us.map.gz — i.e. Super+Left IS a
# console-switch chord) and where the next keystrokes land in `login(1)`. A
# user's unlock password typed into a getty is recorded in cleartext as a
# failed-login *username* in the journal and btmp. That exact leak was observed
# in the CI corpus (`FAILED LOGIN 1 FROM tty1`) before the test lane was fixed
# in 9f8af8d.
#
# seatd installs K_OFF when the compositor takes the seat, so the guarantee
# holds as long as nothing else opens and resets the VT. A getty does exactly
# that: its start-time TTY reset reverts K_OFF.
#
# The production hazard is NOT that a getty is *enabled* on the compositor VT
# (it is not). It is that logind autospawns one ON DEMAND: the compositor VT
# (tty3) is inside logind's default NAutoVTs=6 range, so whenever the VT is
# free — greetd stopped, compositor wedged or crash-looping, i.e. exactly
# doc/recovery.md scenario A — a switch to it makes logind start
# autovt@tty3.service. Verified live 2026-07-25: with greetd stopped, `chvt 2
# && chvt 3` spawned agetty on tty3 and flipped its keyboard mode from K_OFF
# back to K_UNICODE. That also contradicts doc/recovery.md's "no qdistro
# text-mode VT login", because the prompt appears on the *graphical* VT.
# Enablement state is irrelevant to that path: logind starts the unit by name.
#
# Masking getty@tty<N> + autovt@tty<N> is the precise fix — logind's StartUnit
# then fails ("Unit autovt@tty3.service is masked") and the VT stays empty.
#
# WHAT THIS DELIBERATELY DOES NOT DO
# ----------------------------------
# It does not set `NAutoVTs=0` / `ReserveVT=0` the way the qdwin GUI *test*
# profile does (scripts/vm/spin-test-vm-gui.sh). That is right for a
# single-purpose test VM and wrong for the product: tty5+ dynamic and pinned
# work sessions are architecture (doc/architecture.md, doc/sessions.md) and
# VT switching is a documented game-session feature (doc/games.md). It is also
# not the security boundary — tty1's agetty is a deliberate emergency console
# (doc/recovery.md), so removing *destinations* can never be the guarantee.
# The guarantee is K_OFF on the compositor VT; this script protects the one
# thing that can take it away. Scoped to the compositor VT only: tty1 and
# tty5+ are untouched.
#
# OFFLINE ROOTS
# -------------
# Also runs inside the kiwi chroot (image/config.sh), where no system manager
# is running. There the mask symlinks are still written into the image's /etc
# and are what the booted image obeys, so the guarantee is unchanged; only the
# runtime probes (stop / is-active) are skipped, because in a chroot systemd
# answers them with a no-op and exit 0 rather than the truth.
#
# LIVE IS THE DEFAULT, AND OFFLINE CANNOT BE ASSERTED INTO EXISTENCE.
# Skipping the runtime probes on a LIVE system would be a security hole: a
# getty already running on the compositor VT holds it with a reset keyboard
# until reboot, and masking does not stop a running instance. So offline
# mode needs BOTH a request and corroboration, and the corroboration is
# positive evidence of a chroot, never mere absence of systemd:
#   * `--offline` (an argument, which is not inherited by accident) is a
#     caller assertion that must be CORROBORATED by this root; if it is not,
#     that is a caller bug and we exit 2 rather than skip a check.
#   * QDISTRO_OFFLINE_INSTALL=1 (Phase B's environment contract, which CAN
#     leak) is only a request. Corroborated, it selects offline; not
#     corroborated, it is ignored with a warning and the live probes run.
#   * With neither, the live probes ALWAYS run. Detection alone never
#     selects offline: a caller that wants the offline branch must say so.
# Corroboration means `systemd-detect-virt --chroot` (PID 1's root is not
# this root; kiwi bind-mounts the builder's /proc into the image root before
# config.sh runs, so this answers there), or -- for a chroot with no /proc at
# all -- the conjunction of no /proc/1, no /run/systemd/system and
# `systemctl is-system-running` = offline. Any one of those three alone is
# satisfiable on a live machine (a mount namespace hiding /run, a non-systemd
# PID 1 with `offline` printed because the manager is unreachable), so none
# of them is sufficient by itself. A live system in `degraded`, `starting`
# or `maintenance` is live. A chroot that has the host's /run bind-mounted
# reaches the HOST manager, which answers `running`/`degraded`: that case is
# refused (exit 2), i.e. it fails closed rather than open.
#
# Usage: harden-compositor-vt.sh [--offline] [greetd-config.toml]
#   Reads the compositor VT from `[terminal] vt = N`. Defaults to
#   /etc/greetd/config.toml.
# Exit: 0 on success, 1 if the VT is still not exclusively the compositor's
#       afterwards, 2 if the compositor VT could not be determined at all.
#       Non-zero is FAIL-CLOSED on purpose: "I could not work out what to
#       harden" must not be reported to the caller as a hardened install, or
#       the fatal/warn wiring in qdistro-bootstrap.sh and the image build's
#       abort become decorative.

set -uo pipefail

CFG=""
OFFLINE_ASSERTED=0
while [ $# -gt 0 ]; do
    case "$1" in
        --offline) OFFLINE_ASSERTED=1 ;;
        -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
        -*) printf '[harden-vt] WARN: unknown option: %s\n' "$1" >&2 ;;
        *)  CFG="$1" ;;
    esac
    shift
done
CFG="${CFG:-/etc/greetd/config.toml}"

log()  { printf '[harden-vt] %s\n' "$*"; }
warn() { printf '[harden-vt] WARN: %s\n' "$*" >&2; }

# Parse `vt = N` from the [terminal] table only. Section-aware on purpose:
# a loose grep would also match a `vt` key in another table, or a commented
# [initial_session] block — and masking the WRONG VT would brick the login
# path or take out tty1's emergency console.
#
# Tolerates the forms a hand-edited config plausibly uses: `vt=3`, `vt = "3"`,
# CRLF, trailing `#`/`;` comments. Anything else is an ERROR, not a silent
# skip: a `vt` key we cannot read means we do not know what to harden.
# Duplicate/conflicting [terminal] tables (invalid TOML, but possible in a
# broken hand edit) are an error rather than a coin flip.
greetd_compositor_vt() {
    local file=$1
    [ -f "$file" ] || return 1
    awk '
        { sub(/\r$/, "") }
        /^[[:space:]]*[#;]/ { next }
        /^[[:space:]]*\[/ {
            section = $0
            sub(/^[[:space:]]*\[[[:space:]]*/, "", section)
            sub(/[[:space:]]*\].*$/, "", section)
            next
        }
        section == "terminal" && /^[[:space:]]*vt[[:space:]]*=/ {
            value = $0
            sub(/^[^=]*=[[:space:]]*/, "", value)
            # Quoted values must be closed and followed by nothing but an
            # optional comment. Stripping comments FIRST would accept
            # `vt = "1;junk"` as 1 — a plausible-but-wrong VT, and 1 is
            # tty1, the emergency console we must never mask.
            if (value ~ /^["'"'"']/) {
                quote = substr(value, 1, 1)
                rest  = substr(value, 2)
                endq = index(rest, quote)
                if (endq == 0) {
                    print "unterminated quoted vt value: " $0 > "/dev/stderr"
                    bad = 1
                    next
                }
                trailer = substr(rest, endq + 1)
                if (trailer !~ /^[[:space:]]*([#;].*)?$/) {
                    print "trailing junk after quoted vt value: " $0 > "/dev/stderr"
                    bad = 1
                    next
                }
                value = substr(rest, 1, endq - 1)
            } else {
                sub(/[[:space:]]*[#;].*$/, "", value)
            }
            gsub(/[[:space:]]/, "", value)
            if (value !~ /^[0-9]+$/ || value + 0 <= 0) {
                print "unparsable vt value: " $0 > "/dev/stderr"
                bad = 1
                next
            }
            # tty1 is the deliberate emergency agetty (doc/recovery.md). If the
            # compositor were configured there, hardening would mask the
            # last-resort login. Refuse rather than do that silently.
            if (value + 0 == 1) {
                print "compositor VT is tty1, the emergency console: refusing to mask it" > "/dev/stderr"
                bad = 1
                next
            }
            if (seen && value != found) {
                print "conflicting [terminal] vt values: " found " and " value > "/dev/stderr"
                bad = 1
            }
            found = value
            seen = 1
        }
        END {
            if (bad || !seen) { exit 1 }
            print found
        }
    ' "$file"
}

if ! VT="$(greetd_compositor_vt "$CFG")" || [ -z "${VT:-}" ]; then
    # greetd also accepts vt = "next" / "current". Both are legal config and
    # both make this hardening impossible: the VT is only known at greetd
    # start, so there is no unit name to mask ahead of time. qdistro pins a
    # numeric VT (deploy/greetd-config.toml) precisely so it can be secured.
    if grep -qiE '^[[:space:]]*vt[[:space:]]*=[[:space:]]*["'"'"']?(next|current)' "$CFG" 2>/dev/null; then
        warn "$CFG uses a dynamic 'vt = next/current'; the compositor VT is not knowable at install time"
        warn "pin a numeric '[terminal] vt = N' so getty@ttyN/autovt@ttyN can be masked"
    else
        warn "could not determine the compositor VT from $CFG (want a numeric '[terminal] vt = N')"
    fi
    warn "refusing to report a hardened install: nothing was masked"
    exit 2
fi

log "compositor VT is tty$VT (from $CFG)"

UNITS="getty@tty$VT.service autovt@tty$VT.service"

# Offline root (the kiwi chroot in image/config.sh, or any chroot whose / is
# the target and whose PID 1 is not running)? Then runtime state does not
# exist yet and CANNOT be probed: inside a chroot systemd prints "Running in
# chroot, ignoring command 'is-active'" and exits **0**, so `is-active
# --quiet` reports every unit as running. That false positive aborted the
# first in-repo image build (iso/14 Phase A). In that case assert the
# persistent state instead — which is the only thing that governs what the
# built image does when it actually boots.
#
# offline_root() is the CORROBORATION for an explicit request; it is never
# consulted without one (see the header). It returns 0 only on positive
# evidence that this root is a chroot, not on the absence of a manager.
offline_root() {
    # PID 1's root differs from ours: this root is a chroot. Needs /proc.
    systemd-detect-virt --chroot --quiet 2>/dev/null && return 0
    # No /proc at all, no manager runtime dir, and systemd agrees it cannot
    # reach a manager: a bare chroot. All three together, never one alone.
    if [ ! -e /proc/1/comm ] && [ ! -d /run/systemd/system ] \
       && [ "$(systemctl is-system-running 2>/dev/null)" = offline ]; then
        return 0
    fi
    return 1
}

OFFLINE=0
if [ "$OFFLINE_ASSERTED" = 1 ]; then
    if offline_root; then
        OFFLINE=1
    else
        # --offline is a caller assertion about the root it is pointed at.
        # Being wrong about that would silently skip the live checks, so
        # refuse. This also covers a chroot with the host's /run bind-mounted
        # (the host manager answers, so the chroot is not corroborated).
        warn "--offline was given, but this root is not corroborated as a chroot"
        warn "refusing to skip the runtime checks on a possibly live system"
        exit 2
    fi
elif [ "${QDISTRO_OFFLINE_INSTALL:-0}" = 1 ]; then
    if offline_root; then
        OFFLINE=1
    else
        warn "QDISTRO_OFFLINE_INSTALL=1 in the environment, but this root is not"
        warn "corroborated as a chroot; ignoring it and running the live checks"
    fi
fi

if [ "$OFFLINE" = 1 ]; then
    log "offline root (corroborated chroot, no running system manager): masking only, runtime probes skipped"
fi

# Stop first: masking an already-running instance leaves it running (and
# holding the VT with a reset keyboard) until the next boot. Nothing runs in
# an offline root, and `stop` there is the same chroot no-op as `is-active`.
if [ "$OFFLINE" = 0 ]; then
    for unit in $UNITS; do
        if systemctl is-active --quiet "$unit" 2>/dev/null; then
            log "stopping $unit (it is holding the compositor VT)"
            systemctl stop "$unit" 2>/dev/null || warn "could not stop $unit"
        fi
    done
fi

# `systemctl mask` is idempotent (it reports "Created symlink" only the first
# time) and upgrades a runtime-only mask to a persistent one. Disable first so
# a stale enablement symlink from an older install is cleared rather than left
# shadowed by the mask.
for unit in $UNITS; do
    systemctl disable "$unit" >/dev/null 2>&1 || true
    if ! systemctl mask "$unit" >/dev/null 2>&1; then
        warn "could not mask $unit"
    fi
done

# Verify rather than assume: a mask that silently failed (read-only /etc,
# a conflicting drop-in) must not be reported as a hardened install.
rc=0
for unit in $UNITS; do
    state="$(systemctl is-enabled "$unit" 2>/dev/null || true)"
    if [ "$state" != "masked" ]; then
        warn "$unit is '$state', expected 'masked' — a login prompt can still take tty$VT"
        rc=1
    fi
    # A mask is a symlink to /dev/null. Assert the artifact directly rather
    # than trusting systemctl's answer: in an offline root `is-enabled` is
    # the only one of these that reads the filesystem, and this check holds
    # in both modes, so the mask is verified the same way either way.
    link="$(readlink "/etc/systemd/system/$unit" 2>/dev/null || true)"
    if [ "$link" != "/dev/null" ]; then
        warn "/etc/systemd/system/$unit is not a mask symlink to /dev/null (got '${link:-none}')"
        rc=1
    fi
    # Runtime state is real only where a system manager is running. Offline,
    # there is nothing running to check and systemd answers 0 to every such
    # question; the boot-time guarantee rests on the mask asserted above.
    if [ "$OFFLINE" = 0 ] && systemctl is-active --quiet "$unit" 2>/dev/null; then
        warn "$unit is still active on the compositor VT"
        rc=1
    fi
done

# logind's ReserveVT must not point at the compositor VT: that would mark it
# busy for autovt activation unconditionally, outside the NAutoVTs range.
# Last assignment wins across drop-ins, matching systemd. An unset/commented
# ReserveVT is NOT "no reservation" — systemd's compiled-in default is 6, so
# model that, or a compositor moved to tty6 would sail past this check.
reserve="$(systemd-analyze cat-config systemd/logind.conf 2>/dev/null \
    | awk -F= '
        /^[[:space:]]*ReserveVT[[:space:]]*=/ {
            value = $2
            sub(/[[:space:]]*[#;].*$/, "", value)
            gsub(/[[:space:]]/, "", value)
            if (value ~ /^[0-9]+$/) { v = value }
        }
        END { print v }')"
: "${reserve:=6}"
if [ "$reserve" = "$VT" ]; then
    warn "logind ReserveVT=$reserve is the compositor VT — set it to another VT (or 0)"
    rc=1
fi

if [ "$rc" -eq 0 ]; then
    log "tty$VT is exclusively the compositor's (getty + autovt masked)"
fi
exit "$rc"
