#!/bin/bash
# s122-tier3s-sigkill-cleanup.sh — GUEST driver (root) for
# phase7-tier3s-sigkill-cleanup.bats. Tier 3s teardown when the supervisor
# side goes away, todo/paravirt 06 "Δ DONE bar" item 2 and owner O11:
#   - launcher SIGKILL (service failure): systemd's BindsTo stops the scope
#     while ExecStopPost runs the verified cleanup; nothing is left;
#   - O11 session-manager STOP with two live launches: StopPropagatedFrom
#     stops both launch units (before the manager stops) through the cleanup
#     path; no launch-owned process, scope, token dir or control dir is left;
#   - session-manager crash (SIGKILL) and session-manager restart: the old
#     launch gets a stop JOB (StopPropagatedFrom) and is gone, and the
#     restarted manager relaunches the Active silo with a FRESH token whose
#     record, container and scope are running (not just a new record);
#   - restart reconciliation after the manager's in-memory state is lost:
#     (a) a live launch unit the manager never started (started directly
#     while the manager was down) is stopped and reaped; (b) the same with
#     its control record removed (unrecorded); (c) a live LABELLED container
#     with no launch unit at all is discovered by --reap-stale and reaped.
# Runs after tier3s-guest-setup.sh. One PASS/FAIL line per check;
# `[s122] N passes, M failures`; exit 1 on any failure.
set -u
T3S_TAG=s122
. "$(dirname "$0")/tier3s-guest-lib.sh"
SA=s122a; SB=s122b
MGR=qdistro-session-manager.service
HOLD='["qdistro-tier3s-smoke", "--hold", "600"]'

step "0. preconditions"
is "probe PASS (admin substrate)" "$(/usr/lib/qdistro/tier3s/probe.sh --user admin > /dev/null 2>&1; echo $?)" 0
is "broker allows the smoke spawn" "$(broker_check "$ACTION")" allow
is "launch unit carries StopPropagatedFrom= the manager (O11)" \
    "$(systemctl show -p StopPropagatedFrom --value "$(unit_of $SA)")" "$MGR"
assert_all_clear pre
for s in $SA $SB; do
    sm CreateTier3sSilo ssss "$s" headless-smoke "$s" none > /dev/null; is "CreateTier3sSilo $s" "$(silo_state "$s")" Created
done
# Model A: qt3s-<silo> accounts + per-silo image stores (provisioned by a
# first refused launch inside ensure_silo_image; the image lands in each
# silo's own store — admin's copy is only the staged archive).
for s in $SA $SB; do
    if ensure_silo_image "$s" headless-smoke; then pass "$s: qt3s-$s provisioned; image in its store"
    else fail "$s: ensure_silo_image failed"; fi
done
is "probe PASS as a silo account" \
    "$(/usr/lib/qdistro/tier3s/probe.sh --user "$(silo_acct $SA)" > /dev/null 2>&1; echo $?)" 0
set_argv "$SA=600" "$SB=600" | sed 's/^/    /'
mgr_pid() { systemctl show -p MainPID --value "$MGR"; }
mgr_journal() { journalctl -u "$MGR" --no-pager -o cat --after-cursor="$1" 2>/dev/null; }

# ---------------------------------------------------------------------------
step "1. launcher SIGKILL (service failure), three times (ExecStopPost races the scope stop)"
for i in 1 2 3; do
    TA=$(up_silo $SA); UA=$(unit_of $SA)
    if [ -n "$TA" ]; then pass "sigkill#$i: launch $TA up"; else fail "sigkill#$i: launch did not come up"; fi
    cur=$(journal_cursor)
    systemctl kill -s KILL --kill-whom=main "$UA"
    wait_for 60 unit_down "$UA"
    is "sigkill#$i: launch unit failed visibly" "$(unit_state "$UA"):$(systemctl show -p Result --value "$UA")" "failed:signal"
    unit_log "$UA" "$cur" | grep -v pam_unix | sed 's/^/    unit: /'
    assert_launch_gone "launcher-sigkill#$i" "$TA" "$SA"
    systemctl reset-failed "$UA"
    sm StopSilo si $SA 10 > /dev/null; is "sigkill#$i: StopSilo afterwards reaches Stopped" "$(silo_state $SA)" Stopped
done

# ---------------------------------------------------------------------------
step "2. O11: session-manager STOP tears down every live launch"
TA=$(up_silo $SA); TB=$(up_silo $SB)
if [ -n "$TA" ] && [ -n "$TB" ]; then pass "manager-stop: two launches up ($TA, $TB)"; else fail "manager-stop: launches did not come up"; fi
cur=$(journal_cursor)
systemctl stop "$MGR"; rc=$?
is "manager-stop: systemctl stop $MGR rc" "$rc" 0
is "manager-stop: manager inactive" "$(unit_state "$MGR")" inactive
for s in $SA $SB; do
    u=$(unit_of "$s"); t=$TA; [ "$s" = "$SB" ] && t=$TB
    is "manager-stop: $u stopped through the verified cleanup" "$(unit_log "$u" "$cur" | grep -c "qdistro-tier3s-cleanup: $t: torn down")" 1
    is "manager-stop: $u stopped before the manager (After= order)" \
        "$([ "$(systemctl show -p InactiveEnterTimestampMonotonic --value "$u")" -le "$(systemctl show -p InactiveEnterTimestampMonotonic --value "$MGR")" ] && echo yes || echo no)" yes
    assert_launch_gone "manager-stop/$s" "$t" "$s"
done
assert_all_clear manager-stop
systemctl start "$MGR"; wait_for 30 manager_up
is "manager-stop: manager started again" "$(yes_no manager_up)" yes
# the silos were Active: the autostart sweep relaunches them with fresh tokens
for s in $SA $SB; do
    old=$TA; [ "$s" = "$SB" ] && old=$TB
    assert_relaunched "manager-stop/$s" "$s" "$old"
    sm StopSilo si "$s" 10 > /dev/null
done
wait_for 30 bash -c '[ -z "$(ls /run/qdistro-tier3s-ctl/ 2>/dev/null)" ]'
assert_all_clear manager-stop-after

# ---------------------------------------------------------------------------
step "3. session-manager crash (SIGKILL) and restart"
for how in crash restart; do
    TA=$(up_silo $SA)
    if [ -n "$TA" ]; then pass "manager-$how: launch $TA up"; else fail "manager-$how: launch did not come up"; fi
    old_pid=$(mgr_pid); cur=$(journal_cursor)
    if [ "$how" = crash ]; then systemctl kill -s KILL "$MGR"; else systemctl restart "$MGR"; fi
    wait_for 60 bash -c "[ \"\$(systemctl show -p MainPID --value $MGR)\" != '$old_pid' ] && [ \"\$(systemctl show -p MainPID --value $MGR)\" != 0 ]"
    wait_for 30 manager_up
    is "manager-$how: manager back with a new pid" "$(yes_no test "$(mgr_pid)" != "$old_pid")" yes
    assert_launch_gone "manager-$how" "$TA" "$SA"
    assert_relaunched "manager-$how" $SA "$TA"
    unit_log "$(unit_of $SA)" "$cur" | grep -E 'Stopping|Stopped|torn down|signal' | sed 's/^/    unit: /'
    # systemd propagates the manager's stop/failure to the launch unit
    # (StopPropagatedFrom=): a stop JOB (fable A r1 P3-4: asserted from PID 1's
    # journal fields) with the verified cleanup, not a kill
    is "manager-$how: systemd completed a stop job for the old launch unit" \
        "$(units_jobs_since "$cur" stop "$(unit_of $SA | sed 's/[.@]/\\&/g')" | sed 's/^[1-9][0-9]*$/yes/')" yes
    is "manager-$how: the old launch got the verified cleanup" \
        "$(unit_log "$(unit_of $SA)" "$cur" | grep -c "qdistro-tier3s-cleanup: $TA: torn down")" 1
    sm StopSilo si $SA 10 > /dev/null; is "manager-$how: StopSilo" "$(silo_state $SA)" Stopped
    wait_for 30 bash -c '[ -z "$(ls /run/qdistro-tier3s-ctl/ 2>/dev/null)" ]'
done
assert_all_clear manager-restarts

# ---------------------------------------------------------------------------
step "4. reconciliation: the manager's in-memory state is lost"
direct_up() {   # direct_up <silo>: start the launch unit WITHOUT the manager -> token
    local s="$1" u cur tok
    u=$(unit_of "$s"); cur=$(journal_cursor)
    write_stanza "$s" "$HOLD" > /dev/null
    systemctl start "$u" || { echo ""; return 1; }
    wait_for 120 bash -c "journalctl -u '$u' --no-pager -o cat --after-cursor='$cur' | grep -q 'spawn-tier3s: running: '" || { echo ""; return 1; }
    tok=$(token_of_unit "$u"); [ -n "$tok" ] || { echo ""; return 1; }
    snapshot_launch "$tok"
    wait_for 60 bash -c "journalctl _SYSTEMD_UNIT=qdistro-tier3s-$tok.scope --no-pager -o cat | grep -q '^SMOKE holding'"
    echo "$tok"
}
for variant in recorded unrecorded; do
    systemctl stop "$MGR"
    TA=$(direct_up $SA)
    if [ -n "$TA" ]; then pass "reconcile/$variant: live launch $TA started while the manager was down"
    else fail "reconcile/$variant: direct launch did not come up"; fi
    is "reconcile/$variant: the launch is live and labelled" \
        "$(ctr_status "$SA"):$(pm_s "$SA" inspect --format '{{index .Config.Labels "qdistro_tier3s_token"}}' "$(ctr_of $SA)")" "running:$TA"
    if [ "$variant" = unrecorded ]; then
        rm -rf "${CTL:?}/$TA"
        is "reconcile/unrecorded: control record removed by hand" "$(yes_no test -e "$CTL/$TA")" no
    fi
    cur=$(journal_cursor)
    systemctl start "$MGR"; wait_for 30 manager_up
    wait_for 60 unit_down "$(unit_of $SA)"
    is "reconcile/$variant: the restarted manager stopped the unknown launch unit" \
        "$(mgr_journal "$cur" | grep -c "tier3s reconciliation: stopping $(unit_of $SA)")" 1
    assert_launch_gone "reconcile/$variant" "$TA" "$SA"
    is "reconcile/$variant: silo not relaunched (it was Stopped)" "$(silo_state $SA)" Stopped
    rm -f "$STANZA_DIR/$SA.env"
    systemctl reset-failed "$(unit_of $SA)" 2>/dev/null
done

step "5. reconciliation: a live labelled container with no launch unit at all"
systemctl stop "$MGR"
GT=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n'); GC=qdistro-tier3s-ghost
# Model A: the reaper scans the qt3s-* stores only — a ghost must live in a
# silo's store, run by the silo account with the same launch argv shape the
# spawn uses (keep-id: guest uid == the silo's host uid).
GSU=$(silo_uid "$SA"); GSG=$(silo_gid "$SA")
is "ghost: silo uid:gid resolved" "$(yes_no test -n "$GSU" -a -n "$GSG")" yes
as_silo "$SA" podman --runtime "$WRAPPER" --runtime-flag=network=none --cgroup-manager=cgroupfs run -d --name "$GC" \
    --label "qdistro_tier3s_token=$GT" --label "qdistro_tier3s_unit=qdistro-tier3s-silo@ghost.service" \
    --security-opt label=disable --security-opt no-new-privileges --cap-drop=ALL \
    --security-opt seccomp=/usr/lib/qdistro/tier3s/seccomp/headless-smoke.json \
    --userns=keep-id --user "$GSU:$GSG" --read-only --network=none \
    "$IMAGE" qdistro-tier3s-smoke --hold 600 > /dev/null 2>&1 < /dev/null
wait_for 60 ctr_running "$SA" "$GC"
gs=$(pm_s "$SA" inspect --format '{{.State.Pid}}' "$GC"); gst=$(starttime "$gs")
is "ghost: labelled container live with no unit and no record" \
    "$(ctr_status "$SA" "$GC"):$(yes_no test -e "$CTL/$GT"):$(unit_state qdistro-tier3s-silo@ghost.service)" "running:no:inactive"
is "ghost: invisible to admin's store" "$(pm container exists "$GC" 2>/dev/null; echo $?)" 1
cur=$(journal_cursor)
systemctl start "$MGR"; wait_for 30 manager_up
wait_for 60 ctr_absent "$SA" "$GC"
pm_s "$SA" container exists "$GC"; rc=$?
is "ghost: reconciliation reaped the labelled container (podman exists rc)" "$rc" 1
pid_gone() { [ "$(starttime "$1" 2>/dev/null)" != "$2" ]; }
wait_for 30 pid_gone "$gs" "$gst"
is "ghost: its sandbox (State.Pid $gs) is gone" "$(yes_no test "$(starttime "$gs" 2>/dev/null)" = "$gst")" no
is "ghost: the manager reported no reconciliation failure" "$(mgr_journal "$cur" | grep -c 'tier3s reconciliation: --reap-stale reported a failure')" 0

step "6. cleanup"
for s in $SA $SB; do sm DeleteSilo s "$s" > /dev/null; is "DeleteSilo $s" "$(silo_state "$s")" absent; done
assert_all_clear end
finish
