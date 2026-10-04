#!/bin/bash
# s127-tier3s-clipboard-gate.sh — GUEST driver (root) for
# phase7-tier3s-clipboard-gate.bats. Phase B (ΔB6) cross-silo clipboard
# gate for the waypipe bridge, tier3s/CONTRACT.md §"Clipboard":
#   - a REAL secctx-tagged selection (qdistro-test-clipboard-source under
#     the same runuser->secctx-exec wrap the bridge client uses) tagged
#     qdistro.tier3s.<sA> is gated by qdshell: the v23
#     selection_set_source_identity sidecar names the tagged source, the
#     dst silo is the focused tier3s toplevel's, and the broker's
#     CheckClipboardTransfer answers the default DENY
#     (CLIPBOARD_GATE ... verdict=deny reason=broker:deny);
#   - strict MIME: an image/png-only offer from a tier3s app_id strips to
#     empty -> deny reason=tier3s-no-allowed-mimes (ΔB6 shares tier-4's
#     text/plain + text/uri-list allow-list);
#   - SaveRule allow on qdistro.clipboard.transfer:sA:sB flips the live
#     verdict: the broker journal shows `rules reloaded (dbus-saverule)`
#     and a second real selection gets verdict=allow reason=broker:allow;
#   - focus-aware clear: while silo B holds the recorded selection,
#     injectFocus onto A's toplevel emits CLIPBOARD_FOCUS_GATE
#     verdict=deny reason=focus-cross-silo;
#   - receive-time gate (broker CheckClipboardReceive): default-deny,
#     then an allow rule with mime_type: text/plain flips text/plain
#     while image/png stays denied;
#   - every cross-silo probe lands an audit row.
# Runs after tier3s-guest-setup.sh --gui. One PASS/FAIL line per check;
# `[s127] N passes, M failures`; exit 1 on any failure.
set -u
T3S_TAG=s127
. "$(dirname "$0")/tier3s-guest-lib.sh"
SA=s127a; SB=s127b
GUISPAWN="qdistro.tier3s.spawn:weston-terminal/weston-terminal"
CLIP_FILE="99-s127-clip.yaml"
CLIP_SRC_PID=""

# kill_clip_src — TERM the runuser wrapper, then sweep any orphaned
# admin-side source still holding the selection (runuser forwards the
# signal, but an orphan must never leave a stale offer for later checks).
kill_clip_src() {
    [ -n "${1:-}" ] && kill -TERM "$1" 2>/dev/null
    wait "$1" 2>/dev/null || :
    # pkill -x would never match: the helper's comm truncates to
    # "qdistro-test-cl". Anchor the full cmdline instead; also drop the
    # secctx-exec parent so no orphan keeps the tagged client alive.
    pkill -u admin -f '^qdistro-test-clipboard-source' 2>/dev/null || :
    pkill -u admin -f '^qdistro-secctx-exec --sandbox-engine qdistro.tier3s .*clipsrc-' 2>/dev/null || :
}

T3S_EXIT_HOOK='
    [ -n "${CLIP_SRC_PID:-}" ] && kill_clip_src "${CLIP_SRC_PID}"
    rm -f "/etc/qdistro/rules.d/'"$CLIP_FILE"'" 2>/dev/null'

step "0. preconditions, silos"
is "probe PASS" "$(/usr/lib/qdistro/tier3s/probe.sh --user admin > /dev/null 2>&1; echo $?)" 0
is "weston-terminal image loaded" "$(yes_no pm image exists localhost/qdistro/tier3s-weston-terminal:latest)" yes
is "admin compositor socket present" "$(yes_no test -S $ADMIN_RT/$GUI_DISPLAY)" yes
is "qdshell is up" "$(as_admin systemctl --user is-active qdshell.service 2>/dev/null)" active
is "clipboard-source helper installed" \
    "$(command -v qdistro-test-clipboard-source 2>/dev/null)" "/usr/bin/qdistro-test-clipboard-source"
is "profile is dev" "$(sed -n 's/^QDISTRO_PROFILE=//p' /etc/qdistro/profile | tail -1)" dev
assert_all_clear pre
for s in $SA $SB; do
    sm CreateTier3sSilo ssss "$s" weston-terminal "$s" none > /dev/null
    is "CreateTier3sSilo $s" "$(silo_state "$s")" Created
done
set_rules "allow:$GUISPAWN"
is "broker answers allow for the GUI spawn" "$(broker_check "$GUISPAWN")" allow
# no clipboard rules yet: default-deny
is "no test clipboard rule present" "$(yes_no test -e /etc/qdistro/rules.d/$CLIP_FILE)" no

step "1. two GUI silos up; focus B's toplevel"
TA=$(up_gui_silo "$SA"); TB=$(up_gui_silo "$SB")
[ -n "$TA" ] && [ -n "$TB" ] && pass "both GUI launches up ($TA, $TB)" \
    || fail "launches did not come up"
HA=$(qs_ipc tier3focus findSiloHandle "$SA" | sed -n 's/^HANDLE=//p')
HB=$(qs_ipc tier3focus findSiloHandle "$SB" | sed -n 's/^HANDLE=//p')
is "findSiloHandle $SA" "${HA:--1}" "$(t3s_window_handle "$SA")"
is "findSiloHandle $SB" "${HB:--1}" "$(t3s_window_handle "$SB")"
is "injectFocus onto $SB's toplevel" \
    "$(qs_ipc tier3focus injectFocus "$HB" default | head -1)" "ok handle=$HB seat=default"
# the gate keys dst_silo off the focused handle — wait until the compositor
# emitted the seat_focus_changed the shell consumed before offering a
# selection, so dst_silo=$SB is deterministic.
wait_for 30 bash -c "journalctl _SYSTEMD_USER_UNIT=qdwin-compositor.service --no-pager -o cat | grep -q 'seat_focus_changed seat=default handle=$HB'" \
    && pass "seat focus landed on $SB's toplevel (handle $HB)" \
    || fail "no seat_focus_changed for handle $HB"

clip_gate_line() {   # clip_gate_line <src_silo> — newest CLIPBOARD_GATE for it
    qdshell_log | grep "CLIPBOARD_GATE .*src_silo=$1 " | tail -1
}

step "2. live tagged selection $SA -> $SB: default deny"
t3s_clip_source "$SA" text/plain "s127-secret-A" > "$WORK/clip-src-A.log" 2>&1 &
CLIP_SRC_PID=$!
wait_for 30 bash -c "journalctl _SYSTEMD_USER_UNIT=qdshell.service --no-pager -o cat | grep -q 'CLIPBOARD_GATE .*src_silo=$SA '"
line=$(clip_gate_line "$SA"); info "gate: $line"
is "qdshell gate line names the real tagged source silo" \
    "$(printf '%s' "$line" | grep -c "src_silo=$SA dst_silo=$SB")" 1
is "default-deny verdict at set-time" \
    "$(printf '%s' "$line" | grep -c 'verdict=deny reason=broker:deny')" 1
is "text/plain offer reached the broker unfiltered" \
    "$(printf '%s' "$line" | grep -c 'mime_types=text/plain')" 1

step "3. strict MIME: image/png-only offer strips to deny"
is "broker probe still denies transfer $SA->$SB" \
    "$(broker_check_clip "$SA" "$SB" "qdistro.tier3s.$SA" "$GUI_ENGINE" 0 0)" deny
t3s_clip_source "$SA" image/png "s127-png" > "$WORK/clip-src-png.log" 2>&1 &
PNG_PID=$!
wait_for 30 bash -c "journalctl _SYSTEMD_USER_UNIT=qdshell.service --no-pager -o cat | grep -q 'verdict=deny reason=tier3s-no-allowed-mimes'"
# the source helper sweeps serials to beat weston's stale-serial guard, so one
# logical offer can emit >1 selection_set; the asserted property is the verdict
# reason, not the wire-event count.
is "qdshell denied a png-only tier3s offer before the broker" \
    "$(qdshell_log | grep -c 'CLIPBOARD_GATE .*verdict=deny reason=tier3s-no-allowed-mimes' | awk '{print ($1>=1)?1:0}')" 1
is "qdshell logged the tier3s mime-strip" \
    "$(qdshell_log | grep -c 'tier3s mime-strip' | awk '{print ($1>=1)?1:0}')" 1
kill_clip_src "$PNG_PID"

step "4. SaveRule flips the live verdict to allow"
RULE_BODY="- name: s127-clip-allow
  decision: allow
  match:
    action: qdistro.clipboard.transfer:$SA:$SB"
is "SaveRule wrote the file" \
    "$(save_rule "$CLIP_FILE" "$RULE_BODY")" "/etc/qdistro/rules.d/$CLIP_FILE"
is "broker logged the saverule reload" \
    "$(broker_log | grep -c 'rules reloaded (dbus-saverule)')" 1
wait_for 20 bash -c "[ \"\$(dbus-send --system --print-reply --dest=org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 org.qdistro.AdminBroker1.CheckClipboardTransfer 'string:$SA' 'string:$SB' array:string:'text/plain' 'string:qdistro.tier3s.$SA' 'string:probe-dst' 'string:$GUI_ENGINE' boolean:false uint32:0 uint64:0 2>/dev/null | grep -oE 'string \"[a-z]+\"' | tail -1)\" = 'string \"allow\"' ]"
is "broker probe allows transfer $SA->$SB under the rule" \
    "$(broker_check_clip "$SA" "$SB" "qdistro.tier3s.$SA" "$GUI_ENGINE" 0 0)" allow
# the same tagged source re-asserts its offer; the gate re-evaluates
kill_clip_src "$CLIP_SRC_PID"
t3s_clip_source "$SA" text/plain "s127-secret-A" > "$WORK/clip-src-A2.log" 2>&1 &
CLIP_SRC_PID=$!
wait_for 30 bash -c "journalctl _SYSTEMD_USER_UNIT=qdshell.service --no-pager -o cat | grep -q 'CLIPBOARD_GATE .*src_silo=$SA .*verdict=allow'"
line=$(clip_gate_line "$SA"); info "gate: $line"
is "live verdict flipped to allow under the rule" \
    "$(printf '%s' "$line" | grep -c 'verdict=allow reason=broker:allow')" 1

step "5. focus-aware clear: selection owned by $SB, focus crosses to $SA"
# Re-focus B and set a B-tagged (denied-but-recorded) selection so
# _selectionSourceSilo tracks B; then move focus to A's toplevel — the
# cross-silo transition must clear and journal CLIPBOARD_FOCUS_GATE.
qs_ipc tier3focus injectFocus "$HB" default > /dev/null
sleep 1
t3s_clip_source "$SB" text/plain "s127-secret-B" > "$WORK/clip-src-B.log" 2>&1 &
BSRC_PID=$!
wait_for 30 bash -c "journalctl _SYSTEMD_USER_UNIT=qdshell.service --no-pager -o cat | grep -q 'CLIPBOARD_GATE .*src_silo=$SB '"
kill_clip_src "$BSRC_PID"
qs_ipc tier3focus injectFocus "$HA" default > /dev/null
wait_for 30 bash -c "journalctl _SYSTEMD_USER_UNIT=qdshell.service --no-pager -o cat | grep -q 'CLIPBOARD_FOCUS_GATE .*src_silo=$SB .*dst_silo=$SA'"
is "focus crossing out of the source silo cleared the selection" \
    "$(qdshell_log | grep -c "CLIPBOARD_FOCUS_GATE .*src_silo=$SB dst_silo=$SA .*verdict=deny reason=focus-cross-silo")" 1
# refocus B so any later checks see a consistent seat state
qs_ipc tier3focus injectFocus "$HB" default > /dev/null; sleep 1

step "6. receive-time gate: default deny, then per-mime allow"
is "receive probe defaults to deny" \
    "$(broker_check_clip_recv "$SA" "$SB" text/plain "qdistro.tier3s.$SA" "$GUI_ENGINE" 0 0)" deny
RECV_FILE="98-s127-recv.yaml"
RECV_BODY="- name: s127-recv-allow
  decision: allow
  match:
    action: qdistro.clipboard.receive:$SA:$SB
    mime_type: text/plain"
is "SaveRule wrote the receive rule" \
    "$(save_rule "$RECV_FILE" "$RECV_BODY")" "/etc/qdistro/rules.d/$RECV_FILE"
wait_for 20 bash -c "[ \"\$(dbus-send --system --print-reply --dest=org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 org.qdistro.AdminBroker1.CheckClipboardReceive 'string:$SA' 'string:$SB' 'string:text/plain' 'string:qdistro.tier3s.$SA' 'string:probe-dst' 'string:$GUI_ENGINE' boolean:false uint32:0 uint64:0 2>/dev/null | grep -oE 'string \"[a-z]+\"' | tail -1)\" = 'string \"allow\"' ]"
is "receive probe allows text/plain under the mime rule" \
    "$(broker_check_clip_recv "$SA" "$SB" text/plain "qdistro.tier3s.$SA" "$GUI_ENGINE" 0 0)" allow
is "receive probe still denies image/png (mime selector)" \
    "$(broker_check_clip_recv "$SA" "$SB" image/png "qdistro.tier3s.$SA" "$GUI_ENGINE" 0 0)" deny

step "7. audit evidence"
n_deny=$(sqlite3 "$AUDIT_DB" "SELECT count(*) FROM audit WHERE action='qdistro.clipboard.transfer:$SA:$SB' AND decision=0;" 2>/dev/null)
n_allow=$(sqlite3 "$AUDIT_DB" "SELECT count(*) FROM audit WHERE action='qdistro.clipboard.transfer:$SA:$SB' AND decision=1;" 2>/dev/null)
n_recv=$(sqlite3 "$AUDIT_DB" "SELECT count(*) FROM audit WHERE action='qdistro.clipboard.receive:$SA:$SB';" 2>/dev/null)
info "audit rows: transfer deny=$n_deny allow=$n_allow recv=$n_recv"
[ "${n_deny:-0}" -ge 1 ] && pass "audit: denied transfer row(s) recorded ($n_deny)" \
    || fail "audit: no denied transfer row for $SA:$SB"
[ "${n_allow:-0}" -ge 1 ] && pass "audit: allowed transfer row(s) recorded ($n_allow)" \
    || fail "audit: no allowed transfer row for $SA:$SB"
[ "${n_recv:-0}" -ge 1 ] && pass "audit: receive probe row(s) recorded ($n_recv)" \
    || fail "audit: no receive row for $SA:$SB"
is "audit: deny row names the default-deny path" \
    "$(sqlite3 "$AUDIT_DB" "SELECT count(*) FROM audit WHERE action='qdistro.clipboard.transfer:$SA:$SB' AND source LIKE 'clipboard_default_deny%';" 2>/dev/null | awk '{print ($1>=1)?"yes":"no"}')" yes

step "8. cleanup"
[ -n "$CLIP_SRC_PID" ] && { kill_clip_src "$CLIP_SRC_PID"; CLIP_SRC_PID=""; }
delete_rule "$CLIP_FILE"; delete_rule "$RECV_FILE"
rm -f "/etc/qdistro/rules.d/$CLIP_FILE" "/etc/qdistro/rules.d/$RECV_FILE"
for s in $SA $SB; do
    sm StopSilo si "$s" 10 > /dev/null
    is "StopSilo $s" "$(silo_state "$s")" Stopped
    wait_for 90 unit_down "$(unit_of "$s")"
done
assert_bridge_gone "cleanup/A" "$TA"; assert_launch_gone "cleanup/A" "$TA" "$(ctr_of "$SA")"
assert_bridge_gone "cleanup/B" "$TB"; assert_launch_gone "cleanup/B" "$TB" "$(ctr_of "$SB")"
for s in $SA $SB; do sm DeleteSilo s "$s" > /dev/null; is "DeleteSilo $s" "$(silo_state "$s")" absent; done
set_rules none
assert_all_clear end
finish
