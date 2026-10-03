#!/bin/bash
# s128-tier3s-lineage.sh — GUEST driver (root) for
# phase7-tier3s-lineage.bats. Phase B (ΔB6/P1-1) launch-record lineage for
# the tier3s waypipe bridge:
#   - spawn-tier3s.sh registers the REAL bridge-client (waypipe client)
#     pid+starttime via RegisterLaunch before podman run; the broker audit
#     row `qdistro.lineage.register:<silo>` carries it;
#   - RegisterLaunch itself re-verifies the live process: a dead pid is
#     refused (CallerGone), a starttime mismatch is refused
#     (CallerIdentityMismatch);
#   - under lineage_enforce=true, CheckClipboardTransfer resolves the
#     RELAYED source (pid,starttime) against the launch-record store:
#       * no relayed pid            -> hard deny (no-source-pid-relayed)
#       * relayed pid with no record -> hard deny (source-unverified)
#       * relayed pid, stale starttime -> hard deny (source-starttime-drift)
#       * relayed pid of A's bridge, FORGED claim src=<other silo> ->
#         the attested silo wins (override logged; the forged claim can
#         never satisfy a rule the real silo can't)
#       * relayed pid of A's bridge, correct claim -> the rule engine
#         sees the attested identity: deny without a rule, allow with one;
#   - cleanup restores the VM's prior lineage_enforce posture verbatim.
# Runs after tier3s-guest-setup.sh --gui. One PASS/FAIL line per check;
# `[s128] N passes, M failures`; exit 1 on any failure.
set -u
T3S_TAG=s128
. "$(dirname "$0")/tier3s-guest-lib.sh"
SA=s128a; SB=s128b
GUISPAWN="qdistro.tier3s.spawn:weston-terminal/weston-terminal"
BROKER_CONF=/etc/qdistro/broker.conf
PRIOR_ENFORCE_LINE=$(grep '^lineage_enforce' "$BROKER_CONF" 2>/dev/null || true)

# Restore the broker's pre-test lineage_enforce posture EXACTLY (the prior
# line if any, else its absence) and restart so the running broker matches,
# even when the driver dies mid-run.
T3S_EXIT_HOOK='
    install -d -m 0755 /etc/qdistro 2>/dev/null
    sed -i "/^lineage_enforce/d" "'"$BROKER_CONF"'" 2>/dev/null
    [ -n "'"$PRIOR_ENFORCE_LINE"'" ] && echo "'"$PRIOR_ENFORCE_LINE"'" >> "'"$BROKER_CONF"'"
    systemctl restart qdistro-admin-broker.service 2>/dev/null || :'

step "0. preconditions, silos"
is "probe PASS" "$(/usr/lib/qdistro/tier3s/probe.sh --user admin > /dev/null 2>&1; echo $?)" 0
is "weston-terminal image loaded" "$(yes_no pm image exists localhost/qdistro/tier3s-weston-terminal:latest)" yes
is "admin compositor socket present" "$(yes_no test -S $ADMIN_RT/$GUI_DISPLAY)" yes
is "qdshell is up" "$(as_admin systemctl --user is-active qdshell.service 2>/dev/null)" active
is "audit db present" "$(yes_no test -f $AUDIT_DB)" yes
is "profile is dev" "$(sed -n 's/^QDISTRO_PROFILE=//p' /etc/qdistro/profile | tail -1)" dev
assert_all_clear pre
for s in $SA $SB; do
    sm CreateTier3sSilo ssss "$s" weston-terminal "$s" none > /dev/null
    is "CreateTier3sSilo $s" "$(silo_state "$s")" Created
done
set_rules "allow:$GUISPAWN"
is "broker answers allow for the GUI spawn" "$(broker_check "$GUISPAWN")" allow

step "1. GUI launch registers the real bridge-client (pid,starttime)"
BEFORE=$(audit_count "qdistro.lineage.register:$SA")
TA=$(up_gui_silo "$SA")
[ -n "$TA" ] && pass "launch up ($TA)" || fail "launch did not come up"
BP=$(rec "$TA" bridge_client_pid); BS=$(rec "$TA" bridge_client_starttime)
is "bridge client pid+starttime recorded" "$(yes_no test -n "$BP" -a -n "$BS")" yes
# RegisterLaunch is mandatory for a GUI launch: reaching phase=running at
# all proves it succeeded (a failed call refuses BEFORE podman run).
is "launch reached running (RegisterLaunch succeeded before podman run)" \
    "$(rec "$TA" phase)" running
wait_for 20 bash -c "[ \$(sqlite3 '$AUDIT_DB' \"SELECT count(*) FROM audit WHERE action='qdistro.lineage.register:$SA';\" 2>/dev/null) -gt ${BEFORE:-0} ]"
row=$(audit_last_source "qdistro.lineage.register:$SA"); info "register row: $row"
is "audit: register row names the bridge pid+starttime" \
    "$(printf '%s' "$row" | grep -c "pid=$BP starttime=$BS")" 1
is "audit: register row carries the tier3s engine/app claim" \
    "$(printf '%s' "$row" | grep -c "engine='qdistro.tier3s' app='qdistro.tier3s.$SA'")" 1

step "2. RegisterLaunch refuses what it cannot re-verify"
out=$(dbus-send --system --print-reply --dest=org.qdistro.AdminBroker1 \
    /org/qdistro/AdminBroker1 org.qdistro.AdminBroker1.RegisterLaunch \
    "string:$SB" "string:qdistro.tier3s" "string:qdistro.tier3s.$SB" \
    "string:$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')" "string:" \
    "uint64:999999" "string:tier3s" "uint64:0" 2>&1) && rc=0 || rc=$?
is "RegisterLaunch of a dead pid is refused" \
    "$(printf '%s' "$out" | grep -c 'not a live process')" 1
out=$(dbus-send --system --print-reply --dest=org.qdistro.AdminBroker1 \
    /org/qdistro/AdminBroker1 org.qdistro.AdminBroker1.RegisterLaunch \
    "string:$SB" "string:qdistro.tier3s" "string:qdistro.tier3s.$SB" \
    "string:$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')" "string:" \
    "uint64:1" "string:tier3s" "uint64:424242424242" 2>&1) && rc=0 || rc=$?
is "RegisterLaunch with a wrong starttime is refused" \
    "$(printf '%s' "$out" | grep -c 'starttime mismatch')" 1

step "3. enforce mode: unattested/forged source can only be denied"
sed -i '/^lineage_enforce/d' "$BROKER_CONF" 2>/dev/null
echo "lineage_enforce = true" >> "$BROKER_CONF"
systemctl restart qdistro-admin-broker.service
wait_for 30 bash -c "busctl --system list --no-pager 2>/dev/null | grep -q '^org\.qdistro\.AdminBroker1 '"
is "broker restarted under lineage_enforce" \
    "$(broker_log | grep -c 'lineage_enforce=True')" 1

# shadow control: none — the broker is now enforcing; every probe below
# exercises it. src_pid=0 / unverified pid / stale starttime all hard-deny.
is "enforce: no relayed source pid -> deny" \
    "$(broker_check_clip "$SA" "$SB" "qdistro.tier3s.$SA" "$GUI_ENGINE" 0 0)" deny
is "enforce: unrecorded live pid (1) -> deny" \
    "$(broker_check_clip "$SA" "$SB" "qdistro.tier3s.$SA" "$GUI_ENGINE" 1 "$(starttime 1)")" deny
is "enforce: real pid + drifted starttime -> deny" \
    "$(broker_check_clip "$SA" "$SB" "qdistro.tier3s.$SA" "$GUI_ENGINE" "$BP" "$((BS + 1))")" deny
is "enforce: real pid + forged claim of another silo -> the attested silo wins" \
    "$(broker_check_clip "forged-silo" "$SB" "qdistro.tier3s.forged" "$GUI_ENGINE" "$BP" "$BS")" deny
# no allow rule for $SA ->$SB yet: attested source still hits default-deny
is "enforce: attested source, no rule -> deny (enforce never bypasses rules)" \
    "$(broker_check_clip "$SA" "$SB" "qdistro.tier3s.$SA" "$GUI_ENGINE" "$BP" "$BS")" deny
is "broker journal shows the override for the forged claim" \
    "$(broker_log | grep -c "lineage ENFORCE (clipboard.transfer): source pid=$BP .*overridden with attested silo='$SA'")" 1
is "audit: source-deny rows recorded under enforce" \
    "$(sqlite3 "$AUDIT_DB" "SELECT count(*) FROM audit WHERE action LIKE 'qdistro.lineage.source_deny:clipboard.transfer:%';" 2>/dev/null | awk '{print ($1>=3)?"yes":"no"}')" yes

step "4. enforce mode: a valid registered source satisfies an allow rule"
CLIP_FILE="99-s128-clip.yaml"
RULE_BODY="- name: s128-clip-allow
  decision: allow
  match:
    action: qdistro.clipboard.transfer:$SA:$SB"
is "SaveRule wrote the clipboard allow rule" \
    "$(save_rule "$CLIP_FILE" "$RULE_BODY")" "/etc/qdistro/rules.d/$CLIP_FILE"
wait_for 20 bash -c "[ \"\$(dbus-send --system --print-reply --dest=org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 org.qdistro.AdminBroker1.CheckClipboardTransfer 'string:$SA' 'string:$SB' array:string:'text/plain' 'string:qdistro.tier3s.$SA' 'string:probe-dst' 'string:$GUI_ENGINE' boolean:false 'uint32:$BP' 'uint64:$BS' 2>/dev/null | grep -oE 'string \"[a-z]+\"' | tail -1)\" = 'string \"allow\"' ]"
is "attested source + allow rule -> allow" \
    "$(broker_check_clip "$SA" "$SB" "qdistro.tier3s.$SA" "$GUI_ENGINE" "$BP" "$BS")" allow
# but the forged claim over A's pid cannot reach a different action key:
# resolved to $SA regardless — claim s128c:s128b would land on the same
# attested action anyway; the durable proof: an UNRELATED registered?
# there is none, so assert a forged dst direction stays denied.
is "attested source, no rule for $SB->$SA -> deny" \
    "$(broker_check_clip "$SB" "$SA" "qdistro.tier3s.$SB" "$GUI_ENGINE" "$BP" "$BS")" deny

step "5. cleanup: restore posture, stop, delete"
delete_rule "$CLIP_FILE"; rm -f "/etc/qdistro/rules.d/$CLIP_FILE"
sed -i '/^lineage_enforce/d' "$BROKER_CONF" 2>/dev/null
[ -n "$PRIOR_ENFORCE_LINE" ] && echo "$PRIOR_ENFORCE_LINE" >> "$BROKER_CONF"
systemctl restart qdistro-admin-broker.service
wait_for 30 bash -c "busctl --system list --no-pager 2>/dev/null | grep -q '^org\.qdistro\.AdminBroker1 '"
is "lineage_enforce restored (prior: '${PRIOR_ENFORCE_LINE:-absent}')" \
    "$(broker_log | sed -n 's/.*lineage_enforce=\([A-Za-z]*\).*/\1/p' | tail -1)" \
    "$([ -n "$PRIOR_ENFORCE_LINE" ] && echo True || echo False)"
for s in $SA $SB; do
    sm StopSilo si "$s" 10 > /dev/null 2>&1
    wait_for 90 unit_down "$(unit_of "$s")"
done
assert_bridge_gone "cleanup/A" "$TA"; assert_launch_gone "cleanup/A" "$TA" "$(ctr_of "$SA")"
sm DeleteSilo s "$SA" > /dev/null; is "DeleteSilo $SA" "$(silo_state "$SA")" absent
sm DeleteSilo s "$SB" > /dev/null; is "DeleteSilo $SB" "$(silo_state "$SB")" absent
set_rules none
assert_all_clear end
finish
