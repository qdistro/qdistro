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
# Usage: harden-compositor-vt.sh [greetd-config.toml]
#   Reads the compositor VT from `[terminal] vt = N`. Defaults to
#   /etc/greetd/config.toml.
# Exit: 0 on success, 1 if the VT is still not exclusively the compositor's
#       afterwards, 2 if the compositor VT could not be determined at all.
#       Non-zero is FAIL-CLOSED on purpose: "I could not work out what to
#       harden" must not be reported to the caller as a hardened install, or
#       the fatal/warn wiring in qdistro-bootstrap.sh and the image build's
#       abort become decorative.

set -uo pipefail

CFG="${1:-/etc/greetd/config.toml}"

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

# Stop first: masking an already-running instance leaves it running (and
# holding the VT with a reset keyboard) until the next boot.
for unit in $UNITS; do
    if systemctl is-active --quiet "$unit" 2>/dev/null; then
        log "stopping $unit (it is holding the compositor VT)"
        systemctl stop "$unit" 2>/dev/null || warn "could not stop $unit"
    fi
done

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
    if systemctl is-active --quiet "$unit" 2>/dev/null; then
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
