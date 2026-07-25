#!/bin/bash
# vt-escape-lockdown.sh — runtime guard for the locked-session VT escape.
#
# Runs INSIDE a production-path VM (greetd -> qdgreeter -> qdwin on tty3;
# `qci snapshot-daily` builds one). Static "no getty is enabled on tty3"
# checks are necessary but not sufficient — logind starts autovt@ttyN by unit
# name on demand, so enablement state proves nothing. This probe measures the
# property that actually matters.
#
# The security property: the console keyboard on the compositor's VT stays
# K_OFF, so keystrokes typed at a LOCKED screen cannot fall through to the
# kernel console (where openSUSE's keymap makes Super+Left a Decr_Console
# switch) and into login(1), which records the unlock password in cleartext as
# a failed-login username in the journal and btmp.
#
# Asserts:
#   1. The compositor VT is the active VT and its KDGKBMODE is K_OFF.
#   2. getty@tty<N> and autovt@tty<N> are masked (so logind's on-demand spawn
#      fails) and not running.
#   3. With the VT deliberately freed, switching to it does NOT spawn a login
#      prompt — the regression this exists to catch. This is the destructive
#      part; it needs --with-freed-vt-experiment because it stops greetd.
#
# Usage: vt-escape-lockdown.sh [--with-freed-vt-experiment]
#
# --with-freed-vt-experiment is DESTRUCTIVE: it stops greetd and terminates the
# admin session. Run it only over qemu-guest-agent / as root on a disposable
# VM — never from an admin session, whose own shell it would kill.
set -uo pipefail

CFG=/etc/greetd/config.toml
RUN_EXPERIMENT=0
case "${1:-}" in
    "")                          ;;
    --with-freed-vt-experiment)  RUN_EXPERIMENT=1 ;;
    *) echo "FAIL: unknown argument '$1' (want --with-freed-vt-experiment)"; exit 64 ;;
esac
[ "$#" -le 1 ] || { echo "FAIL: too many arguments"; exit 64; }

fail() { echo "FAIL: $1"; exit "${2:-1}"; }

# Restore the box whichever way the experiment ends — a fail path must not
# leave the VM parked at a kernel console with greetd down.
EXPERIMENT_STARTED=0
SPARE_VT=""
cleanup() {
    [ "$EXPERIMENT_STARTED" -eq 1 ] || return 0
    # chvt to the spare VT autospawns a getty there; do not leave it running.
    if [ -n "$SPARE_VT" ]; then
        systemctl stop "getty@tty$SPARE_VT.service" "autovt@tty$SPARE_VT.service" 2>/dev/null
    fi
    systemctl start greetd.service 2>/dev/null
}
trap cleanup EXIT

# KDGKBMODE ioctl; K_OFF == 4. Opens O_NOCTTY so reading the mode never steals
# the VT from the compositor.
kbmode() {
    python3 -c "
import fcntl, os, array, sys
fd = os.open('/dev/tty$1', os.O_RDONLY | os.O_NOCTTY)
try:
    buf = array.array('i', [0])
    fcntl.ioctl(fd, 0x4B44, buf, True)
    print(buf[0])
finally:
    os.close(fd)
" 2>/dev/null
}

# Kept deliberately in step with greetd_compositor_vt() in
# scripts/install/harden-compositor-vt.sh — same quoting/comment rules, so the
# probe checks the same VT the installer hardened. Drift fails LOUD (no VT ->
# exit 2), never silently passing on the wrong VT.
VT=$(awk '
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
        if (value ~ /^["'"'"']/) {
            quote = substr(value, 1, 1)
            rest  = substr(value, 2)
            endq = index(rest, quote)
            if (endq == 0) { bad = 1; next }
            trailer = substr(rest, endq + 1)
            if (trailer !~ /^[[:space:]]*([#;].*)?$/) { bad = 1; next }
            value = substr(rest, 1, endq - 1)
        } else {
            sub(/[[:space:]]*[#;].*$/, "", value)
        }
        gsub(/[[:space:]]/, "", value)
        if (value !~ /^[0-9]+$/ || value + 0 <= 1) { bad = 1; next }
        if (seen && value != found) { bad = 1 }
        found = value; seen = 1
    }
    END { if (bad || !seen) { exit 1 } ; print found }' "$CFG" 2>/dev/null)
[ -n "${VT:-}" ] || fail "could not read a usable compositor VT from $CFG" 2
echo "PASS: compositor VT is tty$VT"

# 1. Active VT + K_OFF.
active=$(cat /sys/class/tty/tty0/active 2>/dev/null)
[ "$active" = "tty$VT" ] || fail "active VT is $active, expected tty$VT" 3
echo "PASS: active VT is tty$VT"

mode=$(kbmode "$VT")
[ "$mode" = "4" ] || fail "tty$VT KDGKBMODE=$mode, expected 4 (K_OFF) — the console keyboard is LIVE under the compositor, so a chord can reach login(1)" 4
echo "PASS: tty$VT console keyboard is K_OFF"

# 2. Nothing can take the VT.
for unit in "getty@tty$VT.service" "autovt@tty$VT.service"; do
    state=$(systemctl is-enabled "$unit" 2>/dev/null)
    [ "$state" = "masked" ] || fail "$unit is '$state', expected 'masked' — logind can autospawn it on tty$VT" 5
    ! systemctl is-active --quiet "$unit" 2>/dev/null \
        || fail "$unit is active on the compositor VT" 5
done
echo "PASS: getty@tty$VT + autovt@tty$VT are masked and inactive"

# tty1's emergency agetty must NOT have been collateral damage.
if [ "$VT" != "1" ]; then
    state=$(systemctl is-enabled getty@tty1.service 2>/dev/null)
    [ "$state" = "masked" ] \
        && fail "getty@tty1 is masked — the emergency console (doc/recovery.md) was collateral damage" 6
    echo "PASS: tty1 emergency agetty left intact"
fi

# 3. The regression test proper: free the VT, switch to it, assert no prompt.
if [ "$RUN_EXPERIMENT" -eq 1 ]; then
    [ "$(id -u)" -eq 0 ] || fail "the freed-VT experiment needs root (it stops greetd)" 9
    echo "INFO: freed-VT experiment (stops greetd; disposable VMs only)"
    SPARE_VT=2; [ "$VT" = "2" ] && SPARE_VT=4
    EXPERIMENT_STARTED=1

    systemctl stop greetd.service 2>/dev/null \
        || fail "could not stop greetd to free tty$VT" 9
    loginctl terminate-user admin 2>/dev/null
    # Poll for the VT to actually come free rather than trusting a fixed sleep:
    # leftover session processes on it would otherwise read as a login prompt.
    for _ in $(seq 1 20); do
        ps -eo tty | grep -qx "tty$VT" || break
        sleep 1
    done

    # An unchecked chvt is the difference between a real test and a vacuous
    # PASS: if the switch never happens, logind is never asked for a getty and
    # the assertions below hold trivially.
    chvt "$SPARE_VT" || fail "chvt $SPARE_VT failed; the experiment never ran" 9
    sleep 3
    [ "$(cat /sys/class/tty/tty0/active)" = "tty$SPARE_VT" ] \
        || fail "VT did not switch to tty$SPARE_VT; the experiment never ran" 9
    chvt "$VT" || fail "chvt $VT failed; the experiment never ran" 9
    sleep 4
    [ "$(cat /sys/class/tty/tty0/active)" = "tty$VT" ] \
        || fail "VT did not switch back to tty$VT; the experiment never ran" 9
    echo "PASS: switched away from and back to the freed tty$VT (logind was asked)"

    if systemctl is-active --quiet "getty@tty$VT.service" 2>/dev/null \
       || systemctl is-active --quiet "autovt@tty$VT.service" 2>/dev/null; then
        fail "a getty spawned on the freed compositor VT tty$VT" 7
    fi
    if ps -eo tty | grep -qx "tty$VT"; then
        echo "FAIL: a process (login prompt) appeared on tty$VT:"
        ps -eo pid,tty,comm | awk -v t="tty$VT" '$2 == t'
        exit 7
    fi
    echo "PASS: no login prompt spawned on the freed compositor VT"

    systemctl start greetd.service 2>/dev/null
    for _ in $(seq 1 20); do
        systemctl is-active --quiet greetd.service && break
        sleep 1
    done
    systemctl is-active --quiet greetd.service \
        || fail "greetd did not come back after the experiment (the mask broke the login path)" 8
    echo "PASS: greetd recovered with the mask in place"
    echo "PASS: locked-session VT escape is closed on tty$VT (with freed-VT experiment)"
else
    echo "PASS: locked-session VT escape is closed on tty$VT (static checks only)"
fi
