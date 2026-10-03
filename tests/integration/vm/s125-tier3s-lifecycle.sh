#!/bin/bash
# s125-tier3s-lifecycle.sh — GUEST driver (root) for
# phase7-tier3s-lifecycle.bats. Phase B (ΔB7/ΔB8) GUI lifecycle and the
# pre-podman refusals, tier3s/CONTRACT.md §5 step 12:
#   - a GUI=1 launch with NO admin compositor socket refuses ("no admin
#     compositor socket") BEFORE the broker gate and podman run;
#   - a launch whose bridge client never publishes the launch record
#     (qdistro-secctx-exec stubbed to a no-op — missing launch identity)
#     refuses "did not publish a live pid" before podman run;
#   - a launch whose RegisterLaunch D-Bus call fails (a root-policy drop-in
#     denies the member) refuses "no unregistered GUI launch" before podman
#     run — registration is mandatory for a GUI launch (B-i is stricter
#     than tier 3's warning-only registration);
#   - lifecycle: a live GUI silo stops cleanly; SIGKILL of the launch unit
#     mid-GUI-run tears the bridge + scope down with nothing left;
#   - two concurrent GUI launches come up and go down independently.
# Runs after tier3s-guest-setup.sh --gui. One PASS/FAIL line per check;
# `[s125] N passes, M failures`; exit 1 on any failure.
set -u
T3S_TAG=s125
. "$(dirname "$0")/tier3s-guest-lib.sh"
SA=s125a; SB=s125b
GUISPAWN="qdistro.tier3s.spawn:weston-terminal/weston-terminal"
SECCTX=/usr/bin/qdistro-secctx-exec
DBUS_DROPIN=/etc/dbus-1/system.d/zz-s125-deny-register.conf

# Never leave the VM with the secctx helper moved aside or the deny drop-in
# active if the driver dies mid-run.
T3S_EXIT_HOOK='
    [ -e "'"$SECCTX"'.s125-aside" ] && mv "'"$SECCTX"'.s125-aside" "'"$SECCTX"'" || :
    rm -f "'"$DBUS_DROPIN"'" 2>/dev/null
    systemctl reload dbus 2>/dev/null || dbus-send --system --dest=org.freedesktop.DBus / org.freedesktop.DBus.ReloadConfig >/dev/null 2>&1 || :
    [ -S "'"$ADMIN_RT"'/'"$GUI_DISPLAY"'.moved" ] && mv "'"$ADMIN_RT"'/'"$GUI_DISPLAY"'.moved" "'"$ADMIN_RT"'/'"$GUI_DISPLAY"'" || :'

step "0. preconditions, silos"
is "probe PASS" "$(/usr/lib/qdistro/tier3s/probe.sh --user admin > /dev/null 2>&1; echo $?)" 0
is "weston-terminal image loaded" "$(yes_no pm image exists localhost/qdistro/tier3s-weston-terminal:latest)" yes
is "admin compositor socket present" "$(yes_no test -S $ADMIN_RT/$GUI_DISPLAY)" yes
is "qdshell is up" "$(as_admin systemctl --user is-active qdshell.service 2>/dev/null)" active
is "profile is dev" "$(sed -n 's/^QDISTRO_PROFILE=//p' /etc/qdistro/profile | tail -1)" dev
assert_all_clear pre
for s in $SA $SB; do
    sm CreateTier3sSilo ssss "$s" weston-terminal "$s" none > /dev/null
    is "CreateTier3sSilo $s" "$(silo_state "$s")" Created
done
set_rules "allow:$GUISPAWN"
is "broker answers allow for the GUI spawn" "$(broker_check "$GUISPAWN")" allow

# refused_gui <tag> <silo> <REFUSE substring>: start the launch, expect the
# named refusal, and prove nothing ran — no podman container event, no
# owning scope, no control record or per-launch dir left, no container of
# any kind, no leftover launch record or link.sock.
refused_gui() {
    local tag="$1" silo="$2" want="$3" unit cur t0 rc
    unit=$(unit_of "$silo"); cur=$(journal_cursor); t0=$(date --iso-8601=seconds)
    sleep 1
    sm StartSilo s "$silo" > "$WORK/start.out" 2>&1; rc=$?
    info "$tag: start rc=$rc $(tr '\n' ' ' < "$WORK/start.out" | cut -c1-300)"
    if [ "$rc" -ne 0 ]; then pass "$tag: StartSilo fails for the refused launch (rc=$rc)"
    else fail "$tag: StartSilo returned 0 for a refused launch"; fi
    is "$tag: StartSilo reports the refusal" \
        "$(grep -cF "failed: the launch was refused or failed before it ran" "$WORK/start.out")" 1
    is "$tag: the silo reads Stopped right after the refused start" "$(silo_state "$silo")" Stopped
    wait_for 60 unit_down "$unit"
    sleep 1
    unit_log "$unit" "$cur" | grep -v pam_unix | sed 's/^/    unit: /'
    is "$tag: the spawn refused with the expected message" \
        "$(unit_log "$unit" "$cur" | grep -cF "spawn-tier3s: REFUSE: $want")" 1
    is "$tag: refusal fails the launch unit visibly (exit 2)" \
        "$(systemctl show -p Result --value "$unit"):$(systemctl show -p ExecMainStatus --value "$unit")" "exit-code:2"
    launch_events_since "$t0" | sed 's/^/    podman event: /'
    is "$tag: no podman run (no container event but the probe's scratch create/remove)" \
        "$(launch_events_since "$t0" | grep -c .)" 0
    is "$tag: systemd never started an owning scope" "$(units_started_since "$cur" "$T3S_SCOPE_RE")" 0
    is "$tag: no control record, no per-launch dir" \
        "$(records | grep -c .):$(qry find "$LAUNCHES" -mindepth 1 | grep -c .)" "0:0"
    is "$tag: no container of any kind" "$(qry pm ps -a --format '{{.Names}}' | grep -c .)" 0
    is "$tag: no launch record or link.sock leftover" \
        "$(qry find "$ADMIN_RT" -name 'qdistro-tier3s-launchrec-*' | grep -c .):$(qry find "$LAUNCHES" -name link.sock | grep -c .)" "0:0"
    systemctl reset-failed "$unit" 2>/dev/null
}

step "1. GUI launch refuses when the admin compositor socket is gone"
# Moving wayland-1 aside does not touch live connections (the listener is an
# inode, not the path); only the launch's `-S` precondition sees it.
mv "$ADMIN_RT/$GUI_DISPLAY" "$ADMIN_RT/$GUI_DISPLAY.moved"
is "compositor socket moved aside" "$(yes_no test -S "$ADMIN_RT/$GUI_DISPLAY")" no
refused_gui "no-compositor" "$SA" \
    "GUI workload weston-terminal but no admin compositor socket at $ADMIN_RT/$GUI_DISPLAY"
mv "$ADMIN_RT/$GUI_DISPLAY.moved" "$ADMIN_RT/$GUI_DISPLAY"
is "compositor socket restored" "$(yes_no test -S "$ADMIN_RT/$GUI_DISPLAY")" yes

step "2. missing launch identity: bridge client never publishes a record"
# qdistro-secctx-exec publishes the launch record. A moved-aside binary would
# race the spawn's wrapper-starttime capture (a dead runuser reads as
# "cannot record the bridge wrapper" instead), so swap in a stub that stays
# alive but never publishes: the refusal is deterministically the missing
# launch-identity one.
mv "$SECCTX" "$SECCTX.s125-aside"
printf '#!/bin/sh\n# s125 stub: hold the wrapper open but never publish the launch record\nexec sleep 300\n' > "$SECCTX"
chmod 0755 "$SECCTX"
refused_gui "no-launch-record" "$SA" "the waypipe bridge client did not publish a live pid"
mv -f "$SECCTX.s125-aside" "$SECCTX"
is "secctx helper restored" "$(yes_no test -x "$SECCTX")" yes

step "3. RegisterLaunch failure refuses before podman run"
# A root policy drop-in denies ONLY the RegisterLaunch member — the spawn's
# CheckPermission (admin uid) still passes, so the refusal lands exactly on
# the registration step. deny rules take precedence over the root policy's
# broad allow in dbus policy evaluation.
cat > "$DBUS_DROPIN" <<'XML'
<busconfig>
  <policy user="root">
    <deny send_destination="org.qdistro.AdminBroker1"
          send_member="RegisterLaunch"/>
  </policy>
</busconfig>
XML
chmod 0644 "$DBUS_DROPIN"
systemctl reload dbus 2>/dev/null \
    || dbus-send --system --dest=org.freedesktop.DBus / org.freedesktop.DBus.ReloadConfig > /dev/null
# prove the policy is live before the launch: the same call the spawn makes
out=$(dbus-send --system --print-reply --dest=org.qdistro.AdminBroker1 \
    /org/qdistro/AdminBroker1 org.qdistro.AdminBroker1.RegisterLaunch \
    "string:$SA" "string:qdistro.tier3s" "string:qdistro.tier3s.$SA" \
    "string:00000000000000000000000000000000" "string:" "uint64:1" "string:tier3s" "uint64:0" 2>&1) && rc=0 || rc=$?
if [ "$rc" -ne 0 ]; then pass "control: direct RegisterLaunch is denied under the drop-in (rc=$rc)"
else fail "control: the drop-in did not take effect (RegisterLaunch returned: $out)"; fi
refused_gui "registerlaunch-denied" "$SA" \
    "RegisterLaunch failed for bridge client pid"
rm -f "$DBUS_DROPIN"
systemctl reload dbus 2>/dev/null \
    || dbus-send --system --dest=org.freedesktop.DBus / org.freedesktop.DBus.ReloadConfig > /dev/null

step "4. normal GUI lifecycle: up, toplevel observed, StopSilo, all clear"
TA=$(up_gui_silo "$SA")
[ -n "$TA" ] && pass "lifecycle: launch up ($TA)" || fail "lifecycle: launch did not come up"
is "lifecycle: qdshell observed the toplevel" \
    "$(qdshell_log | grep -c "\[tier3s\] toplevel observed silo=$SA ")" 1
cur=$(journal_cursor)
sm StopSilo si "$SA" 10 > /dev/null
is "lifecycle: StopSilo" "$(silo_state "$SA")" Stopped
wait_for 90 unit_down "$(unit_of "$SA")"
assert_launch_gone "lifecycle" "$TA" "$(ctr_of "$SA")"
assert_bridge_gone "lifecycle" "$TA"

step "5. concurrent launches come up and go down independently"
TA=$(up_gui_silo "$SA"); TB=$(up_gui_silo "$SB")
if [ -n "$TA" ] && [ -n "$TB" ] && [ "$TA" != "$TB" ]; then
    pass "concurrent: two launches up with distinct tokens ($TA, $TB)"
else fail "concurrent: launches did not come up (TA='$TA' TB='$TB')"; fi
[ -n "$(rec "$TA" bridge_client_pid)" ] \
    && [ "$(rec "$TA" bridge_client_pid)" != "$(rec "$TB" bridge_client_pid)" ] \
    && pass "concurrent: bridge client pids differ ($(rec "$TA" bridge_client_pid) vs $(rec "$TB" bridge_client_pid))" \
    || fail "concurrent: same or missing bridge client pid"
# the -o client unlinks link.sock at accept — the live proof is each
# client's established stream on its own launch's bound path.
is "concurrent: both bridge channels live (distinct link.sock streams)" \
    "$(bridge_stream_live "$TA" && bridge_stream_live "$TB" && echo yes || echo no)" yes
is "concurrent: qdshell observed both toplevels" \
    "$(qdshell_log | grep -c '\[tier3s\] toplevel observed silo=s125[ab] ')" 2
# kill the launch unit under A (BindsTo the scope): B must survive
systemctl kill -s KILL --kill-whom=main "$(unit_of "$SA")"
wait_for 90 unit_down "$(unit_of "$SA")"
is "concurrent: A's launch unit failed visibly (signal)" \
    "$(unit_state "$(unit_of "$SA")"):$(systemctl show -p Result --value "$(unit_of "$SA")")" "failed:signal"
assert_launch_gone "concurrent/A-killed" "$TA" "$(ctr_of "$SA")"
assert_bridge_gone "concurrent/A-killed" "$TA"
is "concurrent: B's launch still runs" "$(rec "$TB" phase)" running
is "concurrent: B's bridge client still live" \
    "$(st=$(rec "$TB" bridge_client_starttime); p=$(rec "$TB" bridge_client_pid); [ "$(starttime "$p" 2>/dev/null)" = "$st" ] && echo yes || echo no)" yes
is "concurrent: B's toplevel still in the qdshell model" \
    "$(qs_ipc tier3focus findSiloHandle "$SB" 2>/dev/null | grep -cv 'HANDLE=-1')" 1
systemctl reset-failed "$(unit_of "$SA")" 2>/dev/null
sm StopSilo si "$SA" 10 > /dev/null
sm StopSilo si "$SB" 10 > /dev/null; is "concurrent: StopSilo B" "$(silo_state "$SB")" Stopped
wait_for 90 unit_down "$(unit_of "$SB")"
assert_launch_gone "concurrent/B-stopped" "$TB" "$(ctr_of "$SB")"
assert_bridge_gone "concurrent/B-stopped" "$TB"

step "6. cleanup"
for s in $SA $SB; do sm DeleteSilo s "$s" > /dev/null; is "DeleteSilo $s" "$(silo_state "$s")" absent; done
set_rules none
assert_all_clear end
finish
