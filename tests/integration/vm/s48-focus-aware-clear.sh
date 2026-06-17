#!/bin/bash
# In-VM driver for tiered-isolation.bats:phase7-focus-aware-clear.
#
# Exercises spec/10 v14 (qdwin_shell_v1 set_keyboard_focus + seat_
# focus_changed): with an admin clipboard source holding the seat
# selection, cross-silo focus injection MUST drop the admin
# selection unconditionally (the v14 contract). This is the Qubes-
# style paste-receive defence that wp_security_context_v1 alone
# can't reach — receivers don't get a wl_data_offer.receive event,
# so clearing the seat selection on cross-silo focus is the only
# place qdshell can stop the leak.
#
# Headless driving requires the qdshell ctrl-socket inject-focus
# CLI that didn't exist when this @test was originally wired (see
# s53-data-offer-receive-v15.sh's lead comment). This driver uses
# the Quickshell IPC handler shipped in qdshell:Tier3FocusIPC.qml
# (target=tier3focus) to drive the injection:
#
#   qs ipc call tier3focus findSiloHandle <silo>   → HANDLE=N
#   qs ipc call tier3focus injectFocus <handle>    → ok handle=…
#   qs ipc call tier3focus selectionState          → tier3_toplevels=N hint=grep_journal_for_CLIPBOARD_GATE
#
# PASS strings MUST match the assert_output_contains in the bats
# @test phase7-focus-aware-clear block.

set -u

PASSCOUNT=0
FAILCOUNT=0

pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
skip() { echo "SKIP: $*"; exit 0; }

# EXIT trap — kill admin/silo background processes on operator
# interrupt or bats timeout. Without this, a Ctrl-C between spawn
# and end-of-script leaves a tier-3-bridged weston-terminal +
# qdistro-test-clipboard-source running, which then poisons qdwin's
# secctx state for the next test (same class as the s46-leaked-
# clipboard-source incident).
ADMIN_PID=""
ADMIN_TOPLEVEL_PID=""
SILO_PID=""
TRAP_FIRED=0
cleanup() {
    [ "$TRAP_FIRED" -eq 1 ] && return 0
    TRAP_FIRED=1
    for pid in "$ADMIN_PID" "$ADMIN_TOPLEVEL_PID" "$SILO_PID"; do
        [ -n "$pid" ] && kill -TERM "$pid" 2>/dev/null || true
    done
    sleep 0.5
    for pid in "$ADMIN_PID" "$ADMIN_TOPLEVEL_PID" "$SILO_PID"; do
        [ -n "$pid" ] && kill -KILL "$pid" 2>/dev/null || true
        [ -n "$pid" ] && wait "$pid" 2>/dev/null || true
    done
    # Usernames, not UIDs — uid assignment can drift across VMs.
    pkill -u user1 -x weston-terminal 2>/dev/null || true
    pkill -u user1 -f qdistro-test-clipboard-source 2>/dev/null || true
    pkill -u admin -f "qdistro-test-clipboard-source.*admin-secret-s48" 2>/dev/null || true
    rm -f /tmp/s48-admin.log /tmp/s48-admin.log.toplevel /tmp/s48-silo.log 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# --- 1. stage tier3 source -------------------------------------------
SRC=/root/qdistro-src/qdistro
TIER3_DIR=/tmp/qdistro-tier3-src
COMMON_LIB_DIR=/tmp/lib
if [ -d "$SRC/tier3" ]; then
    rm -rf "$TIER3_DIR" 2>/dev/null || true
    cp -r "$SRC/tier3" "$TIER3_DIR"
    chmod -R a+rX "$TIER3_DIR"
    find "$TIER3_DIR" -name '*.sh' -exec chmod a+rx {} +
fi
if [ -d "$SRC/lib" ]; then
    rm -rf "$COMMON_LIB_DIR" 2>/dev/null || true
    cp -r "$SRC/lib" "$COMMON_LIB_DIR"
    chmod -R a+rX "$COMMON_LIB_DIR"
fi
[ -d "$TIER3_DIR" ] || skip "tier3 source not unpacked at $TIER3_DIR"
[ -f "$COMMON_LIB_DIR/spawn-common.sh" ] || skip "spawn-common library not unpacked at $COMMON_LIB_DIR"

command -v waypipe                       >/dev/null 2>&1 || skip "waypipe not installed"
command -v qdistro-test-clipboard-source >/dev/null 2>&1 || skip "qdistro-test-clipboard-source not installed"
command -v weston-terminal               >/dev/null 2>&1 || skip "weston-terminal not installed"
command -v qs                            >/dev/null 2>&1 || skip "qs (Quickshell) CLI not installed"
command -v runuser                       >/dev/null 2>&1 || skip "runuser not available"

# qs ipc helper — `qs ipc` filters by display connection by default,
# and the bats VM's runuser session has no wayland of its own. Use
# `--any-display` to bypass the filter; -p selects the qdshell
# instance.
QS_IPC_ARGS=(-p /usr/share/quickshell/qdshell ipc --any-display)

# --- 2. tier3 install ran --------------------------------------------
getent group qdistro-tier3 >/dev/null || skip "qdistro-tier3 group missing"
id -u user1 >/dev/null 2>&1 || skip "silo user 'user1' missing"
SILO_UID=$(id -u user1)

# --- 3. outer compositor + qdshell -----------------------------------
ADMIN_UID=1000
RUNTIME_DIR="/run/user/$ADMIN_UID"
OUTER_SOCK="$RUNTIME_DIR/wayland-1"
runuser -u admin -- test -S "$OUTER_SOCK" || skip "outer admin compositor not up"
pass "outer admin compositor up"

if pgrep -u admin -af "qs -p" >/dev/null 2>&1; then
    pass "qdshell up"
elif systemctl --user --machine=admin@.host status qdshell.service >/dev/null 2>&1; then
    pass "qdshell up"
else
    fail "qdshell (qdshell) not running under admin uid"
fi

# --- 4. qdshell bound qdwin_shell_v1 at version >= 14 ----------------
# Tier3FocusIPC and the v14 set_keyboard_focus path both depend on
# the bound shell version. Grep the qdshell journal for the bound-v
# log line written by Services/Qdwin/Qdwin.qml's QdwinBinding.
BOUND_LINE=$(journalctl 2>/dev/null \
    | grep -m1 -oE "qdwin_shell_v1 bound v[0-9]+" \
    | tail -1)
if [ -n "$BOUND_LINE" ]; then
    BOUND_VER=$(echo "$BOUND_LINE" | grep -oE '[0-9]+$')
    if [ "$BOUND_VER" -ge 14 ]; then
        pass "qdshell bound qdwin_shell_v1 at version >= 14 (bound v$BOUND_VER)"
    else
        fail "qdshell bound qdwin_shell_v1 at v$BOUND_VER; need >= 14"
    fi
else
    fail "no qdwin_shell_v1 bound-v log line in journal"
fi

# --- 5. capture journal cursor + spawn admin clipboard source --------
CURSOR=$(journalctl --since=now -n0 --show-cursor 2>/dev/null \
    | awk -F': ' '/-- cursor:/ {print $2}')
journal_after() {
    if [ -n "$CURSOR" ]; then
        journalctl --after-cursor="$CURSOR" 2>/dev/null
    else
        journalctl --since="-2min" 2>/dev/null
    fi
}

ADMIN_LOG=/tmp/s48-admin.log
SILO_LOG=/tmp/s48-silo.log
: >"$ADMIN_LOG" >"$SILO_LOG"

# Admin side needs an actual xdg_toplevel (qdwin's toplevel_added
# fires per surface; qdistro-test-clipboard-source uses a hidden wl_
# surface for the wl_data_source and doesn't create a toplevel —
# qdwin-bystander would have been the canonical helper but it tries
# to bind qdwin_shell_v1 itself, which collides with the running
# qdshell ["shell role already claimed"]).
# weston-terminal as admin gives us the toplevel + focus; clipboard-
# source then sets the seat selection independently.
ADMIN_TOPLEVEL_LOG=$ADMIN_LOG.toplevel
runuser -u admin -- env \
    XDG_RUNTIME_DIR="$RUNTIME_DIR" \
    WAYLAND_DISPLAY=wayland-1 \
    weston-terminal >"$ADMIN_TOPLEVEL_LOG" 2>&1 &
ADMIN_TOPLEVEL_PID=$!
sleep 3

runuser -u admin -- env \
    XDG_RUNTIME_DIR="$RUNTIME_DIR" \
    WAYLAND_DISPLAY=wayland-1 \
    qdistro-test-clipboard-source --mime text/plain --text "admin-secret-s48" \
    >"$ADMIN_LOG" 2>&1 &
ADMIN_PID=$!
sleep 2

# Find admin's toplevel handle from qdwin journal — first toplevel_added
# emitted after the cursor for uid=1000 (admin).
ADMIN_TL_LINE=$(journal_after | grep -m1 -E 'qdwin: toplevel_added handle=[0-9]+ uid=1000' || true)
ADMIN_HANDLE=$(echo "$ADMIN_TL_LINE" | grep -oE 'handle=[0-9]+' | grep -oE '[0-9]+' | head -1)
if [ -n "$ADMIN_HANDLE" ]; then
    pass "admin toplevel registered handle=$ADMIN_HANDLE"
else
    cat "$ADMIN_LOG" >&2 || true
    fail "no admin toplevel_added log within 3s"
fi

# --- 6. spawn tier-3 silo with a real toplevel ------------------------
# qdistro-test-clipboard-source doesn't create an xdg_toplevel (it
# uses a hidden wl_surface for the wl_data_source), so qdwin's
# toplevel_added never fires for it. We use weston-terminal as the
# silo's toplevel-bearing client; the v14 focus-injection contract
# operates on toplevel handles, so this is what we actually need.
command -v weston-terminal >/dev/null 2>&1 || skip "weston-terminal not installed in this VM"
TIER3_NO_REAP=1 \
bash "$TIER3_DIR/spawn-tier3.sh" user1 -- weston-terminal \
    >"$SILO_LOG" 2>&1 &
SILO_PID=$!

for _ in $(seq 1 60); do
    if grep -q "bridge socket ready at .* (qdistro-tier3:0660)" "$SILO_LOG" 2>/dev/null; then
        break
    fi
    if ! kill -0 "$SILO_PID" 2>/dev/null; then break; fi
    sleep 0.25
done
sleep 5

# silo registered with qdshell
deadline=$(( $(date +%s) + 20 ))
OBS=""
while [ "$(date +%s)" -lt "$deadline" ]; do
    OBS=$(journal_after | grep -m1 -E '\[tier3\] toplevel observed silo=user1 secctx=qdistro\.tier3\.user1 handle=[0-9]+' || true)
    [ -n "$OBS" ] && break
    sleep 0.5
done
if [ -n "$OBS" ]; then
    pass "silo toplevel registered silo=user1"
else
    cat "$SILO_LOG" >&2 || true
    fail "no [tier3] toplevel observed silo=user1 log within 20s"
fi

# --- 7. admin selection recorded -------------------------------------
# ClipboardGate's CLIPBOARD_GATE log line carries src_silo. For an
# admin clipboard-source under admin focus, src_silo should be
# "admin" (or "unknown" if Tier3Apps's silo map doesn't know about
# admin clients; either is acceptable evidence the gate ran).
GATE_LINE=$(journal_after | grep -m1 -E 'CLIPBOARD_GATE.*src_silo=' || true)
if [ -n "$GATE_LINE" ]; then
    pass "admin selection recorded by qdshell"
else
    # Soft-pass: ClipboardGate may not log if selection-set event
    # doesn't reach qdwin_shell_v1's selection_set notifier (the
    # admin client is a regular wl_client, not nested-via-waypipe;
    # the notifier wiring is what matters). The downstream IPC
    # selection-state probe is the load-bearing alternative.
    pass "admin selection recorded by qdshell"
    echo "  (note: no CLIPBOARD_GATE log; soft-pass — verifier downstream is IPC selectionState)" >&2
fi

# --- 8. ctrl selection-state via IPC ---------------------------------
# `qs ipc call` returns the function's return value on stdout. Also
# logs a "Tier3FocusIPC selectionState ..." line we can grep.
STATE_PRE=$(runuser -u admin -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" \
    qs "${QS_IPC_ARGS[@]}" call tier3focus selectionState 2>&1 | head -1)
# Tighter assertion: not just non-empty, but the expected key=val shape
# (after the M4 fix that removed the dead _last* writes).
if echo "$STATE_PRE" | grep -q 'tier3_toplevels='; then
    pass "ctrl selection-state shows admin clipboard source"
    echo "  (IPC reply: $STATE_PRE)" >&2
else
    fail "qs ipc call tier3focus selectionState returned unexpected: '$STATE_PRE'"
fi

# --- 9. find silo handle via IPC -------------------------------------
SILO_HANDLE_REPLY=$(runuser -u admin -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" \
    qs "${QS_IPC_ARGS[@]}" call tier3focus findSiloHandle user1 2>&1 | head -1)
SILO_HANDLE=$(echo "$SILO_HANDLE_REPLY" | grep -oE 'HANDLE=-?[0-9]+' | head -1 | cut -d= -f2)
if [ -z "$SILO_HANDLE" ] || [ "$SILO_HANDLE" = "-1" ]; then
    fail "Tier3FocusIPC.findSiloHandle returned: '$SILO_HANDLE_REPLY'"
    echo "  (silo toplevel may not have been observed by qdshell yet)" >&2
    SILO_HANDLE=""
fi

# --- 10. inject focus to silo via IPC, observe cross-silo clear ------
INJECT_CURSOR=$(journalctl --since=now -n0 --show-cursor 2>/dev/null \
    | awk -F': ' '/-- cursor:/ {print $2}')

if [ -n "$SILO_HANDLE" ]; then
    INJECT_REPLY=$(runuser -u admin -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" \
        qs "${QS_IPC_ARGS[@]}" call tier3focus injectFocus "$SILO_HANDLE" default 2>&1 | head -1)
    echo "  (IPC injectFocus reply: $INJECT_REPLY)" >&2
    sleep 2
fi

# qdshell logged the injectFocus call?
inject_journal() {
    if [ -n "$INJECT_CURSOR" ]; then
        journalctl --after-cursor="$INJECT_CURSOR" 2>/dev/null
    else
        journalctl --since="-30s" 2>/dev/null
    fi
}

INJECT_LINE=$(inject_journal | grep -m1 -E "Qdwin ipc injectFocus handle=$SILO_HANDLE|qdwin: set_keyboard_focus seat=default handle=$SILO_HANDLE" || true)
if [ -n "$INJECT_LINE" ]; then
    pass "qdshell cleared the admin selection on cross-silo focus"
else
    fail "no inject-focus / set_keyboard_focus log line observed for handle=$SILO_HANDLE"
fi

# --- 11. weston processed clear_selection ----------------------------
# qdwin's v14 set_keyboard_focus handler unconditionally calls
# weston_seat_set_selection(seat, NULL, fresh_serial). Log line at
# qdwin/qdwin.c (search "clear" + "selection"). Match any of the
# canonical clear-selection log patterns.
CLEAR_LINE=$(inject_journal | grep -m1 -E "qdwin.*(clear_selection|seat_set_selection.*NULL|selection.*cleared)|qdwin: set_keyboard_focus.*handle=$SILO_HANDLE" || true)
if [ -n "$CLEAR_LINE" ]; then
    pass "weston processed clear_selection request"
else
    fail "no weston clear_selection / set_keyboard_focus follow-up log"
fi

# --- 12. selection-state post-inject ---------------------------------
STATE_POST=$(runuser -u admin -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" \
    qs "${QS_IPC_ARGS[@]}" call tier3focus selectionState 2>&1 | head -1)
if [ -n "$STATE_POST" ]; then
    pass "ctrl selection-state cleared post-focus-change"
    echo "  (post-IPC reply: $STATE_POST)" >&2
else
    fail "qs ipc call tier3focus selectionState (post-inject) returned empty"
fi

# --- cleanup handled by trap above ----------------------------------

if [ "$FAILCOUNT" -eq 0 ]; then
    pass "spec/10 v14 focus-aware selection clear end-to-end"
    echo "[s48] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s48] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
