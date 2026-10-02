#!/bin/bash
# Deterministic smoke: while the lock UI is up, qdlocker has an fprintd
# VerifyStart in flight, and a fingerprint match unlocks with an EMPTY password
# field (sessions.md "fingerprint = the owner is present").
#
# Executable replacement for 02-fprintd-fallback.md (visual:none). Same
# assertions, against qdlocker's ctrl-socket introspection and the compositor:
#   1.1 after Ctrl+Alt+L: `locked=True prompt-len=0`
#   2.1 after the fake fprintd emits VerifyStatus("verify-match", true):
#       unlock-result `last=success` with prompt-len still 0 (no key typed)
#   2.2 the compositor released the lock: `qdwin: locked_changed=0` in the
#       qdwin-compositor journal after the match (the scenario asked "qdwin ctrl
#       reports the lock surface was destroyed" through the retired qdshell.py
#       ctrl socket; the compositor's own lock-state transition is the live
#       equivalent)
# Setup widens fprintd_timeout_s to 120 s through a root-owned locker.conf (as
# the scenario did) and RESTORES the previous config afterwards.
#
# Exit: 0 pass; 1 assertion failed; 2 setup/environment failure.
set -uo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/qdlocker-helpers.sh" || { echo "SETUP: cannot source qdlocker-helpers.sh" >&2; exit 2; }
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"
export VMNAME
# shellcheck source=../../../qdwin/tests/lib/journal-smoke.sh
source "$QDWIN_REPO/tests/lib/journal-smoke.sh"

CONF=/etc/qdistro/locker.conf
BACKUP=/tmp/qdlocker-fprintd-smoke.conf.bak
MISSING=/tmp/qdlocker-fprintd-smoke.conf.missing

cleanup() {
    local rc=$?
    qdlocker_drain_lock_state >/dev/null 2>&1 || true
    js_guest "if [ -f $MISSING ]; then rm -f $CONF; elif [ -f $BACKUP ]; then install -m 0644 -o 0 -g 0 $BACKUP $CONF; fi
rm -f $BACKUP $MISSING
systemctl stop qdistro-fprintd-fake.service 2>/dev/null || true
runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart qdlocker.service; sleep 2" >/dev/null 2>&1
    exit "$rc"
}

js_guest 'test -x /usr/libexec/qdistro-fprintd-fake && systemctl cat qdistro-fprintd-fake.service >/dev/null 2>&1 && echo OK' | grep -qx OK \
    || js_setup_fail "the fake fprintd (qdistro-fprintd-fake.service) is not staged on this image"
trap cleanup EXIT

js_guest "set -e
systemctl stop fprintd.service 2>/dev/null || true
systemctl restart qdistro-fprintd-fake.service
sleep 1
install -d -m 0755 -o 0 -g 0 /etc/qdistro
if [ -f $CONF ]; then cp -p $CONF $BACKUP; else : > $MISSING; fi
printf 'fprintd_timeout_s = 120\n' > $CONF
chown 0:0 $CONF; chmod 0644 $CONF
echo FPRINT-SETUP-OK" | grep -qx FPRINT-SETUP-OK || js_setup_fail "could not start the fake fprintd / install locker.conf"

# qdlocker_session_healthy enables introspection, prepares the lane and
# restarts qdlocker (which reloads locker.conf).
qdlocker_session_healthy >/dev/null || js_setup_fail "qdlocker session not healthy"
qdlocker_drain_lock_state >/dev/null || js_setup_fail "could not drain a stale lock"
qdwin_release_modifiers

# Step 1 — engage the locker
qdwin_chord ctrl alt -- l
qdlocker_wait_for_lock 10 || js_fail "1.1: Ctrl+Alt+L did not lock (status: $(qdlocker_ctrl status 2>/dev/null))"
st=$(qdlocker_ctrl status 2>/dev/null)
case "$st" in
    *locked=True*prompt-len=0*|*prompt-len=0*locked=True*) js_pass "1.1 $(printf '%s' "$st" | grep -oE 'locked=True|prompt-len=0' | tr '\n' ' ')" ;;
    *) js_fail "1.1: expected locked=True prompt-len=0, got: $st" ;;
esac

# Step 2 — fingerprint match, no password typed
C=$(js_cursor); [ -n "$C" ] || js_setup_fail "no journal cursor"
js_guest 'busctl --system call net.reactivated.Fprint /net/reactivated/Fprint/Device/0 qdistro.FprintFake EmitMatch && echo EMIT-OK' \
    | grep -qx EMIT-OK || js_setup_fail "fake fprintd EmitMatch call failed"
qdlocker_wait_for_unlock 10 || js_fail "2.1: no last=success after the fingerprint match (unlock-result: $(qdlocker_ctrl unlock-result 2>/dev/null))"
st=$(qdlocker_ctrl status 2>/dev/null)
case "$st" in
    *prompt-len=0*) ;;
    *) js_fail "2.1: prompt-len is not 0 — something was typed: $st" ;;
esac
case "$st" in
    *locked=False*) ;;
    *) js_fail "2.1: qdlocker still reports locked after last=success: $st" ;;
esac
js_pass "2.1 last=success with prompt-len=0 (fingerprint-only unlock)"
js_wait "$C" 'qdwin: locked_changed=0' 10 >/dev/null \
    || js_fail "2.2: the compositor never logged 'qdwin: locked_changed=0' after the fingerprint match"
js_pass "2.2 compositor released the lock (locked_changed=0)"

echo "PASS: fprintd match unlocks with an empty password field"
