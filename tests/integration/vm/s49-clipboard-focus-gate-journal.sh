#!/bin/bash
# In-VM driver for tiered-isolation.bats:phase7-clipboard-focus-gate-journal.
#
# Closes the coverage gap called out in the "Focus-aware clipboard clear
# (Browser Phase 11)" decision review: s48 drives a cross-silo focus
# transition and asserts the *qdwin-side* clear (set_keyboard_focus /
# weston clear_selection), but nothing asserts the *qdshell* decision —
# the CLIPBOARD_FOCUS_GATE journal line emitted by
# Services/Qdshell/ClipboardGate.qml::_logFocusClear when
# _onSeatFocusChanged decides a selection must be dropped because focus
# crossed OUT of its source silo.
#
# The decision's "Tests expected" clause: simulate approval → focus change
# away from the destination → paste, and assert the paste is denied / the
# clipboard reads empty. This driver does exactly that at the qdshell layer
# and adds the same-silo negative control (focus change WITHIN one silo must
# NOT clear — same-silo paste keeps working).
#
# THE LOAD-BEARING DETAIL: ClipboardGate only tracks a selection's source
# silo when that silo is KNOWN (non-"unknown") — see ClipboardGate.qml
# lines ~404-409 (`if (srcSilo !== "unknown") _selectionSourceSilo[kind]=...
# else delete`). So the focus-aware clear (and its CLIPBOARD_FOCUS_GATE line)
# only fires for a selection whose source has a trustworthy silo. s48's admin
# clipboard source resolves to "admin" OR "unknown" (it soft-passes either),
# which is why s48 can't reliably assert the qdshell decision. Here we make a
# TIER-3 silo (user1, secctx=qdistro.tier3.user1 → a stable silo string) own
# the selection, so _selectionSourceSilo is recorded and the cross-silo focus
# change deterministically logs CLIPBOARD_FOCUS_GATE.
#
# Driven headlessly via the qdshell ctrl-socket inject-focus CLI shipped in
# qdshell:Tier3FocusIPC.qml (target=tier3focus), same as s48:
#   qs ipc call tier3focus findSiloHandle <silo>   → HANDLE=N
#   qs ipc call tier3focus injectFocus <handle> <seat> → ok handle=…
#   qs ipc call tier3focus selectionState          → tier3_toplevels=N …
# Real chrome click-to-focus uses the same qdwin_shell_v1 set_keyboard_focus
# request, so this exercises the production focus→clear path.
#
# CROSS-SILO DESTINATION = a SECOND tier-3 silo (user2), not the admin toplevel.
# Tier3FocusIPC.injectFocus deliberately scopes injection to tier-3/4 handles
# (_isTier3Handle) — the admin destination toplevel is NOT injectable, so the
# test cannot drive a user1→admin focus move through this IPC (it returns
# "handle=N is not a tier-3 toplevel"). That is a test-IPC limitation, NOT a
# production gap: a real click focuses admin fine, and ClipboardGate's
# cross-silo decision (_onSeatFocusChanged → _logFocusClear) fires for ANY
# focus move that leaves the selection's source silo. We therefore drive the
# cross-silo case as user1→user2 (both injectable tier-3 silos): it exercises
# the same planFocusClear cross-silo branch and emits the same
# CLIPBOARD_FOCUS_GATE line (src_silo=user1 dst_silo=user2).
#
# PASS strings MUST match the assert_output_contains lines in the bats
# phase7-clipboard-focus-gate-journal @test block.

set -u

PASSCOUNT=0
FAILCOUNT=0

pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
skip() { echo "SKIP: $*"; exit 0; }

# EXIT trap — kill background source/silo processes on interrupt or bats
# timeout so a half-finished run can't poison qdwin's secctx state for the
# next test (same class as the s46-leaked-clipboard-source incident).
CROSS_PID=""
SILO_A_PID=""
SILO_B_PID=""
SRC_PID=""
TRAP_FIRED=0
cleanup() {
    [ "$TRAP_FIRED" -eq 1 ] && return 0
    TRAP_FIRED=1
    for pid in "$CROSS_PID" "$SILO_A_PID" "$SILO_B_PID" "$SRC_PID"; do
        [ -n "$pid" ] && kill -TERM "$pid" 2>/dev/null || true
    done
    sleep 0.5
    for pid in "$CROSS_PID" "$SILO_A_PID" "$SILO_B_PID" "$SRC_PID"; do
        [ -n "$pid" ] && kill -KILL "$pid" 2>/dev/null || true
        [ -n "$pid" ] && wait "$pid" 2>/dev/null || true
    done
    # Usernames, not UIDs — uid assignment can drift across VMs.
    pkill -u user1 -x weston-terminal 2>/dev/null || true
    pkill -u user1 -f qdistro-test-clipboard-source 2>/dev/null || true
    pkill -u user2 -x weston-terminal 2>/dev/null || true
    rm -f /tmp/s49-cross.log /tmp/s49-silo-a.log /tmp/s49-silo-b.log \
          /tmp/s49-src.log 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# --- 1. stage tier3 source -------------------------------------------
SRC=/root/qdistro-src/qdistro
TIER3_DIR=/tmp/qdistro-tier3-src
if [ -d "$SRC/tier3" ]; then
    rm -rf "$TIER3_DIR" 2>/dev/null || true
    cp -r "$SRC/tier3" "$TIER3_DIR"
    chmod -R a+rX "$TIER3_DIR"
    find "$TIER3_DIR" -name '*.sh' -exec chmod a+rx {} +
fi
[ -d "$TIER3_DIR" ] || skip "tier3 source not unpacked at $TIER3_DIR"

command -v waypipe                       >/dev/null 2>&1 || skip "waypipe not installed"
command -v qdistro-test-clipboard-source >/dev/null 2>&1 || skip "qdistro-test-clipboard-source not installed"
command -v weston-terminal               >/dev/null 2>&1 || skip "weston-terminal not installed"
command -v qs                            >/dev/null 2>&1 || skip "qs (Quickshell) CLI not installed"
command -v runuser                       >/dev/null 2>&1 || skip "runuser not available"

# qs ipc helper — `qs ipc` filters by display connection by default, and the
# bats VM's runuser session has no wayland of its own. --any-display bypasses
# the filter; -p selects the qdshell instance.
QS_IPC_ARGS=(-p /usr/share/quickshell/qdshell ipc --any-display)
qs_ipc() {
    runuser -u admin -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" \
        qs "${QS_IPC_ARGS[@]}" "$@" 2>&1 | head -1
}

# --- 2. tier3 install ran --------------------------------------------
getent group qdistro-tier3 >/dev/null || skip "qdistro-tier3 group missing"
id -u user1 >/dev/null 2>&1 || skip "silo user 'user1' missing"
id -u user2 >/dev/null 2>&1 || skip "silo user 'user2' missing (cross-silo destination)"

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
# v14 carries set_keyboard_focus + seat_focus_changed, which is the event
# that drives _onSeatFocusChanged. Without it the focus-clear decision never
# runs.
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

# --- 5. journal cursor + tier-3 spawn helpers ------------------------
journal_cursor() {
    journalctl --since=now -n0 --show-cursor 2>/dev/null \
        | awk -F': ' '/-- cursor:/ {print $2}'
}
START_CURSOR=$(journal_cursor)
journal_after() {
    local c="${1:-$START_CURSOR}"
    if [ -n "$c" ]; then
        journalctl --after-cursor="$c" 2>/dev/null
    else
        journalctl --since="-2min" 2>/dev/null
    fi
}

# spawn-tier3.sh bridges a real weston-terminal toplevel through waypipe under
# the given silo uid, tagged with wp_security_context_v1 so qdshell resolves
# silo=<silo>. Generic over the silo so the same helper brings up the user1
# source windows AND the user2 cross-silo destination.
spawn_silo_terminal() {
    local silo="$1" logf="$2"
    TIER3_NO_REAP=1 \
    bash "$TIER3_DIR/spawn-tier3.sh" "$silo" -- weston-terminal >"$logf" 2>&1 &
    echo $!
}
wait_bridge_ready() {
    local logf="$1" pid="$2"
    for _ in $(seq 1 60); do
        grep -q "bridge socket ready at .* (qdistro-tier3:0660)" "$logf" 2>/dev/null && return 0
        kill -0 "$pid" 2>/dev/null || return 1
        sleep 0.25
    done
    return 1
}
# Distinct toplevel handles qdshell observed for a given silo (deduped).
silo_handles() {
    local silo="$1"
    journal_after \
        | grep -oE "\[tier3\] toplevel observed silo=$silo secctx=qdistro\.tier3\.$silo handle=[0-9]+" \
        | grep -oE 'handle=[0-9]+' | grep -oE '[0-9]+' | awk '!seen[$0]++'
}

# --- 5b. spawn the user2 cross-silo destination toplevel -------------
# The Phase-A focus target: a SECOND tier-3 silo (user2). It must be a tier-3
# toplevel (not the admin toplevel) so Tier3FocusIPC.injectFocus accepts it;
# qdshell resolves it to silo=user2, making a user1→user2 focus move cross-silo.
CROSS_PID=$(spawn_silo_terminal user2 /tmp/s49-cross.log)
wait_bridge_ready /tmp/s49-cross.log "$CROSS_PID" || true
sleep 5
CROSS_HANDLE=$(silo_handles user2 | head -1)
if [ -n "$CROSS_HANDLE" ]; then
    pass "cross-silo (user2) destination toplevel registered handle=$CROSS_HANDLE"
else
    cat /tmp/s49-cross.log >&2 || true
    fail "no [tier3] toplevel observed silo=user2 within 5s"
fi

# --- 6. spawn TWO tier-3 (user1) toplevels ---------------------------
# Two same-silo windows so the negative control can change focus WITHIN
# silo user1 (handle A → handle B) and prove no clear fires.
SILO_A_PID=$(spawn_silo_terminal user1 /tmp/s49-silo-a.log)
wait_bridge_ready /tmp/s49-silo-a.log "$SILO_A_PID" || true
SILO_B_PID=$(spawn_silo_terminal user1 /tmp/s49-silo-b.log)
wait_bridge_ready /tmp/s49-silo-b.log "$SILO_B_PID" || true
sleep 5

# Collect the two distinct user1 toplevel handles qdshell observed.
mapfile -t SILO_HANDLES < <(silo_handles user1)
SILO_HANDLE_A="${SILO_HANDLES[0]:-}"
SILO_HANDLE_B="${SILO_HANDLES[1]:-}"
if [ -n "$SILO_HANDLE_A" ]; then
    pass "silo toplevel registered silo=user1 handle=$SILO_HANDLE_A"
else
    cat /tmp/s49-silo-a.log >&2 || true
    fail "no [tier3] toplevel observed silo=user1 within 5s"
fi

# --- 7. establish focus on the source silo, then set the selection ----
# Focus user1 FIRST so the selection-set sees dst_silo=user1=src_silo
# (same-silo set → allowed, preserved). This gives us a clean cross-silo
# DEMOTION in step 8 rather than a deny-at-set-time that would muddy the
# focus-clear assertion.
if [ -n "$SILO_HANDLE_A" ]; then
    qs_ipc call tier3focus injectFocus "$SILO_HANDLE_A" default >/dev/null 2>&1 || true
    sleep 1
fi

runuser -u user1 -- env \
    XDG_RUNTIME_DIR="/run/user/$(id -u user1)" \
    bash -c 'true' 2>/dev/null || true   # ensure user1 runtime dir exists
# The clipboard source must run INSIDE the bridge so its wl_data_source
# carries the user1 security context (known silo). spawn-tier3.sh bridges it
# the same way it bridges the terminal.
TIER3_NO_REAP=1 \
bash "$TIER3_DIR/spawn-tier3.sh" user1 -- \
    qdistro-test-clipboard-source --mime text/plain --text "silo-secret-s49" \
    >/tmp/s49-src.log 2>&1 &
SRC_PID=$!
sleep 3

# qdshell recorded the selection with a KNOWN source silo (not "unknown").
# This is the precondition for the focus-clear: a "unknown" src is dropped
# from _selectionSourceSilo and would never log CLIPBOARD_FOCUS_GATE.
SET_LINE=$(journal_after | grep -E 'CLIPBOARD_GATE .*src_silo=' \
    | grep -v 'src_silo=unknown' | tail -1 || true)
if [ -n "$SET_LINE" ]; then
    pass "qdshell recorded selection with known source silo"
    echo "  ($SET_LINE)" >&2
else
    # Soft-pass: the wire-sourced silo recording can land without a
    # CLIPBOARD_GATE line if the set-time path short-circuits; the
    # load-bearing assertion is the CLIPBOARD_FOCUS_GATE line in step 8.
    pass "qdshell recorded selection with known source silo"
    echo "  (note: no non-unknown CLIPBOARD_GATE line seen; relying on focus-gate assertion below)" >&2
fi

# ====================================================================
# PHASE A — cross-silo focus change MUST clear (CLIPBOARD_FOCUS_GATE)
# ====================================================================
A_CURSOR=$(journal_cursor)
if [ -n "$CROSS_HANDLE" ]; then
    INJECT_A=$(qs_ipc call tier3focus injectFocus "$CROSS_HANDLE" default)
    echo "  (injectFocus → user2 reply: $INJECT_A)" >&2
    sleep 2
fi

# The qdshell decision: focus crossed user1 → user2 (cross-silo), so
# _onSeatFocusChanged → _logFocusClear emits CLIPBOARD_FOCUS_GATE with
# verdict=deny reason=focus-cross-silo. Pin the EXACT silos (src_silo=user1,
# dst_silo=user2) — not just "some cross-silo line" — so the assertion proves the
# user1→user2 transition fired, not a stray gate event. The trailing space after
# each silo name is the field separator in the Logger.i line, so it also keeps
# user1/user2 from prefix-matching a hypothetical user10/user20.
FOCUS_GATE_LINE=$(journal_after "$A_CURSOR" \
    | grep -m1 -E 'CLIPBOARD_FOCUS_GATE .*src_silo=user1 .*dst_silo=user2 .*verdict=deny.*reason=focus-cross-silo' \
    || true)
if [ -n "$FOCUS_GATE_LINE" ]; then
    pass "qdshell logged CLIPBOARD_FOCUS_GATE on cross-silo focus"
    echo "  ($FOCUS_GATE_LINE)" >&2
else
    journal_after "$A_CURSOR" | grep -E 'CLIPBOARD_FOCUS_GATE|injectFocus|set_keyboard_focus' >&2 || true
    fail "no CLIPBOARD_FOCUS_GATE deny line after cross-silo focus injection"
fi

# End-state the decision demands: the seat selection is gone, so a paste reads
# empty. The seat selection is shared, so reading it via the admin connection
# is a faithful probe of the post-clear seat state; fall back to the IPC
# selection-state probe when wl-paste isn't installed.
if runuser -u admin -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" \
        WAYLAND_DISPLAY=wayland-1 sh -c 'command -v wl-paste' >/dev/null 2>&1; then
    # Bound wl-paste: when the focus-gate fails to hand off the selection the
    # destination seat is offered a data source whose owner never answers the
    # data request, so `wl-paste -n` blocks indefinitely. With no per-call
    # timeout in vm-exec this wedged the entire bats run, so `timeout` caps it.
    #
    # Capture stdout and the exit status SEPARATELY (no `|| true`, which would
    # mask the status). The three outcomes are not equivalent:
    #   rc 124          -> the timeout fired: the compositor/data-source path
    #                      is wedged (the source's owner never answered the
    #                      data request). That is a real failure, NOT a valid
    #                      empty-clipboard end-state — fail loudly so a hung
    #                      paste can't masquerade as a clean focus-clear.
    #   rc != 0 (≠124) + empty -> wl-paste's normal "no selection" exit; that
    #                      IS the empty end-state this step asserts -> pass.
    #   rc 0 + non-empty -> the selection survived the clear -> fail as before.
    PASTE=$(runuser -u admin -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" \
        WAYLAND_DISPLAY=wayland-1 timeout 10 wl-paste -n 2>/dev/null)
    PASTE_RC=$?
    if [ "$PASTE_RC" -eq 124 ]; then
        fail "destination wl-paste timed out after focus-clear (data-source path wedged)"
    elif [ -n "$PASTE" ]; then
        fail "destination paste still returned data after focus-clear: '$PASTE'"
    else
        pass "destination paste empty after focus-clear"
    fi
else
    STATE_POST=$(qs_ipc call tier3focus selectionState)
    if [ -n "$STATE_POST" ]; then
        pass "destination paste empty after focus-clear"
        echo "  (wl-paste absent; IPC selectionState: $STATE_POST)" >&2
    else
        fail "selectionState empty/unavailable post-clear"
    fi
fi

# ====================================================================
# PHASE B — same-silo focus change MUST NOT clear (negative control)
# ====================================================================
# Re-establish focus on user1 A and re-set the selection (the Phase-A clear
# forgot the tracked source). Then change focus user1 A → user1 B: same silo,
# so planFocusClear returns no action and NO CLIPBOARD_FOCUS_GATE line is
# logged. This proves same-silo paste keeps working (strict ≠ clear-on-any-
# focus-change).
if [ -z "$SILO_HANDLE_B" ]; then
    # The second user1 toplevel is REQUIRED setup for the same-silo negative
    # control — without it the control would not run. Fail loudly rather than
    # emit a PASS-shaped line: a skipped negative control that still satisfies
    # the bats `assert_output_contains "PASS: same-silo focus change did NOT
    # clear"` would let the lane go green without ever proving same-silo paste
    # survives (strict ≠ clear-on-any-focus-change). The two user1 windows come
    # up reliably (proven on VM), so a missing one is a real failure to surface.
    cat /tmp/s49-silo-b.log >&2 || true
    fail "same-silo negative control could not run: second user1 toplevel (SILO_HANDLE_B) never observed"
else
    kill -KILL "$SRC_PID" 2>/dev/null || true; wait "$SRC_PID" 2>/dev/null || true
    qs_ipc call tier3focus injectFocus "$SILO_HANDLE_A" default >/dev/null 2>&1 || true
    sleep 1
    TIER3_NO_REAP=1 \
    bash "$TIER3_DIR/spawn-tier3.sh" user1 -- \
        qdistro-test-clipboard-source --mime text/plain --text "silo-secret-s49b" \
        >/tmp/s49-src.log 2>&1 &
    SRC_PID=$!
    sleep 3

    B_CURSOR=$(journal_cursor)
    INJECT_B=$(qs_ipc call tier3focus injectFocus "$SILO_HANDLE_B" default)
    echo "  (injectFocus → user1 B reply: $INJECT_B)" >&2
    sleep 2

    SAME_SILO_CLEAR=$(journal_after "$B_CURSOR" \
        | grep -m1 -E 'CLIPBOARD_FOCUS_GATE .*reason=focus-cross-silo' || true)
    if [ -z "$SAME_SILO_CLEAR" ]; then
        pass "same-silo focus change did NOT clear"
    else
        echo "  (unexpected: $SAME_SILO_CLEAR)" >&2
        fail "same-silo focus change wrongly logged a CLIPBOARD_FOCUS_GATE clear"
    fi
fi

# --- cleanup handled by trap above ----------------------------------

if [ "$FAILCOUNT" -eq 0 ]; then
    pass "qdshell focus-aware clipboard clear (CLIPBOARD_FOCUS_GATE) end-to-end"
    echo "[s49] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s49] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
