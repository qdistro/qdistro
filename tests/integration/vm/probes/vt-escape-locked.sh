#!/bin/bash
# vt-escape-locked.sh — the locked-session half of the VT-escape guard.
#
# Runs INSIDE the production-path VM that vt-escape-lockdown.bats converts
# (greetd -> autologin qdwin session on tty3), as root, in two phases with
# REAL kernel keyboard input injected from the host (`virsh send-key`) between
# them:
#
#   pre   policy checks, lock the session, record baselines
#   post  assert the injected keys changed nothing, then switch VT away and
#         back and assert it again
#
# vt-escape-lockdown.sh measures K_OFF on an UNLOCKED session. The claim in
# doc/threat-model.md that K_OFF holds "across greeter, active session and
# locked session" was a one-off manual check (2026-07-25); this makes the
# locked half a guard. It also pins the two kernel paths that revert K_OFF
# WITHOUT a getty (doc/threat-model.md overclaimed "the one thing"):
#   * SysRq bit 4 (unRaw `r` -> vt_reset_unicode, SAK `k` -> do_SAK +
#     reset_vc). SysRq is an input handler upstream of kbd_keycode, so K_OFF
#     does not gate it; only the kernel.sysrq mask does. Bit 64 (term/kill
#     all) would kill seatd/compositor/locker. Neither may be enabled.
#   * A keymap-bound K_SAK: kbd_keycode honours it even under K_OFF,
#     independent of the SysRq mask. No shipped kbd keymap binds SAK; only a
#     root loadkeys could add one, and this asserts none is loaded.
#
# The distro keymap's Decr_Console/Incr_Console chords are deliberately KEPT
# (todo/open-followups.md "Decisions 2026-10-12"); K_OFF is what neuters them,
# and this measures that it does at the locked screen.
#
# Usage: vt-escape-locked.sh pre|post
# State between phases lives in $STATE (root-only).
set -uo pipefail

PHASE=${1:-}
STATE=/run/vt-escape-locked
SENTINEL=vtprobesentinel
ADMIN_UID=1000
SOCK=/run/user/$ADMIN_UID/qdlocker.sock
MARKER=/etc/qdistro/locker-ctrl-introspection

fail() { echo "FAIL: $1"; exit "${2:-1}"; }
[ "$(id -u)" -eq 0 ] || fail "must run as root (KDGKBMODE, btmp, journal)" 64

# Compositor VT: the bats setup_file already asserted greetd says vt = 3 and
# tty3 is active; the probe re-derives it from the active VT at `pre` and pins
# it in $STATE so `post` compares against the same VT.
kbmode() {
    python3 -c "
import fcntl, os, array
fd = os.open('/dev/tty$1', os.O_RDONLY | os.O_NOCTTY)
try:
    buf = array.array('i', [0])
    fcntl.ioctl(fd, 0x4B44, buf, True)   # KDGKBMODE
    print(buf[0])
finally:
    os.close(fd)
" 2>/dev/null
}

# Count keymap entries bound to K_SAK (KT_SPEC=2, index 15 -> 0x020f; 0x020c
# is K_BOOT, the ordinary Ctrl+Alt+Del binding) across
# every table/keycode via KDGKBENT on a console fd. Global keymap, so any VT's
# fd reads the same table. Prints the count; non-zero exit on ioctl failure.
sak_bindings() {
    python3 -c "
import fcntl, os, struct
K_SAK = 0x020f
fd = os.open('/dev/tty1', os.O_RDONLY | os.O_NOCTTY)
n = 0
try:
    for table in range(256):          # MAX_NR_KEYMAPS
        for index in range(256):      # NR_KEYS
            buf = bytearray(struct.pack('BBH', table, index, 0))
            fcntl.ioctl(fd, 0x4B46, buf, True)   # KDGKBENT
            if struct.unpack('BBH', bytes(buf))[2] == K_SAK:
                n += 1
finally:
    os.close(fd)
print(n)
"
}

locker() {
    runuser -u admin -- bash -c "printf '%s\n' '$1' | socat -t 2 - UNIX-CONNECT:$SOCK" 2>/dev/null
}

# login(1) only records failures in an EXISTING btmp, so `pre` guarantees the
# file exists; any later read error is a failure, never a silent 0.
# Called in $(...): report on stderr (a stdout FAIL would be swallowed by the
# substitution) and let the caller exit with the status.
btmp_size() { stat -c %s /var/log/btmp || { echo "FAIL: cannot stat /var/log/btmp" >&2; return 6; }; }

# Journal since the pre-phase cursor, with the read's own status checked: a
# failed query must not read as "no leak". Extra args (e.g. -k) pass through.
journal_since() {
    local out
    out=$(journalctl --after-cursor="$(cat "$STATE/cursor")" --no-pager -o cat "$@") \
        || { echo "FAIL: journalctl --after-cursor $* failed; cannot assert on the journal" >&2; return 6; }
    printf '%s\n' "$out"
}

# Undo this probe's test-only state on every exit after it starts changing
# things: remove the introspection marker unless it pre-existed (the running
# locker keeps introspection until its next restart; the file is what a
# preserved VM would otherwise carry), and clear the failed-unlock tally.
restore() {
    [ -f "$STATE/marker-preexisting" ] || rm -f "$MARKER"
    faillock --user admin --reset 2>/dev/null || true
}
restore_on_failure() {
    local rc=$?
    [ "$rc" -eq 0 ] || restore
}

assert_locked_vt() {   # <label>
    local vt active mode st
    vt=$(cat "$STATE/vt")
    active=$(cat /sys/class/tty/tty0/active 2>/dev/null)
    [ "$active" = "tty$vt" ] \
        || fail "$1: active VT is $active, expected tty$vt — the locked compositor was abandoned" 3
    mode=$(kbmode "$vt")
    [ "$mode" = "4" ] \
        || fail "$1: tty$vt KDGKBMODE=$mode, expected 4 (K_OFF) — console keyboard is LIVE under the locked screen" 4
    st=$(locker status)
    case "$st" in
        *locked=True*) ;;
        *) fail "$1: qdlocker no longer reports locked (${st:-no reply})" 5 ;;
    esac
    echo "PASS: $1: still on tty$vt, K_OFF, locked"
}

assert_no_leak() {     # <label>
    local leaked
    local size
    size=$(btmp_size) || exit $?
    [ "$size" = "$(cat "$STATE/btmp")" ] \
        || fail "$1: /var/log/btmp grew — a console login(1) recorded a failed login" 6
    leaked=$(journal_since) || exit $?
    leaked=$(printf '%s\n' "$leaked" | grep -E "FAILED LOGIN|$SENTINEL" | head -3)
    [ -z "$leaked" ] \
        || fail "$1: journal shows a console login leak since lock: $leaked" 6
    echo "PASS: $1: no FAILED LOGIN, no sentinel in the journal, btmp unchanged"
}

case "$PHASE" in
pre)
    # 1. SysRq policy: the mask is the ONLY gate for the K_OFF-reverting keys.
    sysrq=$(cat /proc/sys/kernel/sysrq)
    [ "$sysrq" != "1" ] || fail "kernel.sysrq=1 enables EVERY SysRq function (unRaw/SAK revert K_OFF)" 10
    [ $(( sysrq & 4 )) -eq 0 ] \
        || fail "kernel.sysrq=$sysrq enables bit 4 (unRaw r / SAK k) — both revert K_OFF on the compositor VT" 10
    [ $(( sysrq & 64 )) -eq 0 ] \
        || fail "kernel.sysrq=$sysrq enables bit 64 (term/kill all) — kills seatd, compositor and locker" 10
    [ "$sysrq" -ne 0 ] \
        || fail "kernel.sysrq=0: the injected SysRq keys below would never reach the handler, so the post-phase kernel evidence is unobtainable; this probe assumes the shipped non-zero mask" 10
    echo "PASS: kernel.sysrq=$sysrq excludes bit 4 (unRaw/SAK) and bit 64 (kill all)"

    # 2. No keymap-bound SAK (honoured even under K_OFF).
    n=$(sak_bindings) || fail "could not read the console keymap (KDGKBENT)" 11
    [ "$n" = "0" ] \
        || fail "loaded console keymap binds K_SAK on $n entries — SAK resets the compositor VT even under K_OFF" 11
    echo "PASS: loaded console keymap has no SAK binding"

    # 3. Lock the session for real and prove it. `status` needs the root-owned
    #    introspection marker, read at qdlocker start — same as the GUI
    #    harness's qdwin_guard_idle_locker (ci/lib/guest/gui-waiters.sh).
    rm -rf "$STATE"; install -d -m 0700 "$STATE" || fail "cannot create $STATE" 12
    install -d -m 0755 -o 0 -g 0 /etc/qdistro || fail "cannot create /etc/qdistro" 12
    [ -e "$MARKER" ] && : > "$STATE/marker-preexisting"
    # pre stops at READY with the session locked for the host's injection;
    # restore only if it fails before that. post always restores.
    trap restore_on_failure EXIT
    if ! { : > "$MARKER" && chown 0:0 "$MARKER" && chmod 0644 "$MARKER"; }; then
        fail "cannot install $MARKER" 12
    fi
    faillock --user admin --reset 2>/dev/null || true
    runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/$ADMIN_UID \
        systemctl --user restart qdlocker.service \
        || fail "could not restart qdlocker to enable status introspection" 12
    st=""
    for _ in $(seq 1 30); do
        st=$(locker status); case "$st" in *locked=*) break ;; esac; sleep 0.5
    done
    case "$st" in *locked=*) ;; *) fail "qdlocker status unavailable after restart (${st:-no reply})" 12 ;; esac
    locker lock >/dev/null
    for _ in $(seq 1 30); do
        st=$(locker status); case "$st" in *locked=True*) break ;; esac; sleep 0.5
    done
    case "$st" in *locked=True*) ;; *) fail "session did not lock (${st:-no reply})" 12 ;; esac
    # Let the lock surface take keyboard focus before the host injects keys.
    sleep 2

    active=$(cat /sys/class/tty/tty0/active)
    echo "${active#tty}" > "$STATE/vt"
    journalctl -n 0 --show-cursor --no-pager 2>/dev/null \
        | sed -n 's/^-- cursor: //p' > "$STATE/cursor"
    [ -s "$STATE/cursor" ] || fail "could not record a journal cursor" 12
    if [ ! -e /var/log/btmp ]; then
        install -m 0600 -o 0 -g 0 /dev/null /var/log/btmp \
            || fail "cannot create /var/log/btmp (login(1) only logs to an existing file)" 12
    fi
    btmp_size > "$STATE/btmp" || exit $?
    echo "PASS: session locked on $active (qdlocker locked=True)"
    assert_locked_vt "locked baseline"
    echo "READY: inject keys now"
    ;;
post)
    [ -s "$STATE/vt" ] || fail "no state from the pre phase" 64
    trap restore EXIT

    # The escape itself first, so a regression is reported as what it is (the
    # VT changed / K_OFF lost) rather than as a missing-evidence failure.
    assert_locked_vt "after console-switch chords + SysRq"
    assert_no_leak "after console-switch chords + SysRq"

    # Non-vacuity 1: the injected typing reached the LOCKER (the sentinel +
    # Enter is a failed unlock attempt), so it did not reach a console.
    res=""
    for _ in $(seq 1 20); do
        res=$(locker unlock-result); case "$res" in *last=failed*) break ;; esac; sleep 0.5
    done
    case "$res" in
        *last=failed*) echo "PASS: injected keystrokes reached the lock screen (unlock-result: failed)" ;;
        *) fail "the injected sentinel never reached the locker (${res:-no reply}) — injection did not happen or went elsewhere" 13 ;;
    esac

    # Non-vacuity 2: the SysRq chords reached the kernel handler and were
    # refused by the mask (r and k are bit 4).
    refused=$(journal_since -k) || exit $?
    refused=$(printf '%s\n' "$refused" | grep -c "sysrq: This sysrq operation is disabled")
    [ "${refused:-0}" -ge 2 ] \
        || fail "expected >=2 'sysrq operation is disabled' kernel lines for the injected Alt+SysRq+r/k, saw ${refused:-0} — the SysRq injection did not reach the kernel, so it proves nothing" 14
    echo "PASS: injected SysRq r/k reached the kernel and were refused ($refused)"

    # VT round trip: seatd hands the VT away and back; the compositor must
    # re-arm K_OFF and the session must still be locked when it returns.
    vt=$(cat "$STATE/vt")
    chvt 1 || fail "chvt 1 failed; the round trip never ran" 15
    sleep 3
    [ "$(cat /sys/class/tty/tty0/active)" = "tty1" ] \
        || fail "VT did not switch to tty1; the round trip never ran" 15
    chvt "$vt" || fail "chvt $vt failed" 15
    for _ in $(seq 1 20); do
        [ "$(cat /sys/class/tty/tty0/active)" = "tty$vt" ] && [ "$(kbmode "$vt")" = "4" ] && break
        sleep 0.5
    done
    echo "PASS: switched to tty1 and back to tty$vt"
    assert_locked_vt "after VT round trip"
    assert_no_leak "after VT round trip"

    echo "PASS: locked-session VT escape is closed on tty$vt (locked, injected input)"
    ;;
*)
    fail "usage: vt-escape-locked.sh pre|post" 64
    ;;
esac
