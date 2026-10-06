#!/bin/bash
# s127-tier3s-clipboard-gate.sh — GUEST driver (root) for
# phase7-tier3s-clipboard-gate.bats. Phase B (ΔB6) cross-silo clipboard
# gate for the waypipe bridge, tier3s/CONTRACT.md §"Clipboard", run
# UNDER lineage_enforce: the broker is restarted with
# lineage_enforce=true BEFORE the GUI launches, so the spawn's own
# RegisterLaunch lands in the enforcing store (spawn-created
# registration), and every test clipboard source is registered the same
# way before its offer emits (QDISTRO_CLIP_SRC_DELAY_MS opens the
# window inside the helper's own exe so the record's exe axis holds):
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
#   - SaveRule allow on qdistro.clipboard.transfer:sA:sB takes effect:
#     the broker journal shows `rules reloaded (dbus-saverule)` and the
#     probe flips to allow — then a LIVE cross-silo allow: a tagged
#     sA-source emitting while sB holds focus reaches
#     verdict=allow reason=broker:allow because the v35
#     selection_set_source_peer_identity sidecar relays the SOURCE
#     client's own (pid, starttime) — the attestation does not depend on
#     the source owning the focused toplevel.
#   - live attested ALLOW: a tagged clip source owning a real xdg_toplevel
#     in its own silo (s127c) is focused, its v23 sidecar binds to the
#     toplevel's attested tag, its pid relays to the broker, and the
#     same-silo path allows ONLY after VerifyClientIdentity + the
#     launch-record resolution — the full tagged->bound->registered->
#     attested->allow chain a real tier3s bridge takes.
#   - focus-aware clear: while silo B holds the recorded selection,
#     injectFocus onto A's toplevel emits CLIPBOARD_FOCUS_GATE
#     verdict=deny reason=focus-cross-silo;
#   - receive-time gate (broker CheckClipboardReceive): default-deny,
#     then an allow rule with mime_type: text/plain flips text/plain
#     while image/png stays denied — under enforce the receive lineage
#     chokepoint requires resolved silo-security snapshots, so the test
#     declares [silo.sA]/[silo.sB] in /etc/qdistro/silo-security.toml
#     (restored exactly afterwards);
#   - every cross-silo probe lands an audit row.
# Runs after tier3s-guest-setup.sh --gui. One PASS/FAIL line per check;
# `[s127] N passes, M failures`; exit 1 on any failure.
set -u
T3S_TAG=s127
. "$(dirname "$0")/tier3s-guest-lib.sh"
SA=s127a; SB=s127b; SC=s127c
GUISPAWN="qdistro.tier3s.spawn:weston-terminal/weston-terminal"
CLIP_FILE="99-s127-clip.yaml"
SILO_SEC=/etc/qdistro/silo-security.toml
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

# launch_clip_source <silo> <mime> <text> <logfile> [helper args...] —
# background t3s_clip_source under a per-source QDISTRO_LAUNCH_RECORD_PATH/
# TOKEN pair. $! is only the runuser subshell; the attested wayland client
# is secctx-exec's fork child, whose pid it publishes into the record —
# the same contract spawn's RegisterLaunch path uses.
launch_clip_source() {
    local silo="$1" mime="$2" text="$3" log="$4"; shift 4
    CLIP_LR=$(mktemp -u "$ADMIN_RT/qdistro-tier3s-clipsrcrec-XXXXXXXX.pid")
    CLIP_LR_TOK=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
    QDISTRO_LAUNCH_RECORD_PATH="$CLIP_LR" \
    QDISTRO_LAUNCH_RECORD_TOKEN="$CLIP_LR_TOK" \
        t3s_clip_source "$silo" "$mime" "$text" "$@" >"$log" 2>&1 &
    CLIP_SRC_PID=$!
    # t3s_clip_source derives the secctx instance-id as
    # clipsrc-<silo>-$BASHPID of its own subshell — which IS $! here.
    CLIP_INST="clipsrc-$silo-$CLIP_SRC_PID"
}

# register_clip_launch <silo> — resolve the real client pid out of the
# launch record, then RegisterLaunch it exactly like the spawn does.
register_clip_launch() {
    local silo="$1" cpid
    cpid=$(read_launch_record "$CLIP_LR" "$CLIP_LR_TOK") || return 1
    register_clip_source "$silo" "$CLIP_INST" "$cpid"
}

BROKER_CONF=/etc/qdistro/broker.conf
PRIOR_ENFORCE_LINE=$(grep '^lineage_enforce' "$BROKER_CONF" 2>/dev/null || true)
PRIOR_SILO_SEC_SHA=$(sha256sum "$SILO_SEC" 2>/dev/null | cut -d' ' -f1)

LOCKER_CONF=/etc/qdistro/locker.conf
PRIOR_LOCKER_CONF_SHA=$(sha256sum "$LOCKER_CONF" 2>/dev/null | cut -d' ' -f1)

# Restore the broker's pre-test lineage_enforce posture EXACTLY (the prior
# line if any, else its absence), restore the silo-security registry to
# its exact prior bytes, restore the locker's idle timeout, and restart
# so the running services match — even when the driver dies mid-run.
T3S_EXIT_HOOK='
    [ -n "${CLIP_SRC_PID:-}" ] && kill_clip_src "${CLIP_SRC_PID}"
    rm -f "/etc/qdistro/rules.d/'"$CLIP_FILE"'" 2>/dev/null
    install -d -m 0755 /etc/qdistro 2>/dev/null
    sed -i "/^lineage_enforce/d" "'"$BROKER_CONF"'" 2>/dev/null
    [ -n "'"$PRIOR_ENFORCE_LINE"'" ] && echo "'"$PRIOR_ENFORCE_LINE"'" >> "'"$BROKER_CONF"'"
    [ -f "'"$WORK"'/silo-security.toml.bak" ] && cp "'"$WORK"'/silo-security.toml.bak" "'"$SILO_SEC"'" 2>/dev/null
    if [ -f "'"$WORK"'/locker.conf.bak" ]; then
        cp "'"$WORK"'/locker.conf.bak" "'"$LOCKER_CONF"'" 2>/dev/null
    else
        rm -f "'"$LOCKER_CONF"'" 2>/dev/null
    fi
    systemctl restart qdistro-admin-broker.service 2>/dev/null || :'

# Journal cursor for this run: every grep below is scoped to lines written
# AFTER this point, so a preserved VM's prior-run history (old
# CLIPBOARD_GATE / seat_focus_changed / restart lines) can never satisfy
# or pollute an assertion.
J0=$(journal_cursor)
# wait_for polls conditions through `bash -c` — the log helpers must be
# exported for the child shell to see them.
export -f qdshell_log comp_log broker_log

step "0. preconditions, silos"
is "probe PASS (admin substrate)" "$(/usr/lib/qdistro/tier3s/probe.sh --user admin > /dev/null 2>&1; echo $?)" 0
is "weston-terminal image staged in admin's store" "$(yes_no pm image exists localhost/qdistro/tier3s-weston-terminal:latest)" yes
is "admin compositor socket present" "$(yes_no test -S $ADMIN_RT/$GUI_DISPLAY)" yes
is "qdshell is up" "$(as_admin systemctl --user is-active qdshell.service 2>/dev/null)" active
# injectFocus posts ERROR_LOCKED and is dropped while the compositor is
# locked. The stock idle_timeout_s is 300s — a multi-minute driver can be
# locked mid-run even on a fresh worker (reproduced on a preserved VM:
# the lock fired between poll cycles and every later focus check
# failed). Widen the locker's idle window for the test duration and
# restore it afterwards; this changes NO assertion — it keeps the session
# in the state the focus semantics under test presuppose.
[ -f "$LOCKER_CONF" ] && cp "$LOCKER_CONF" "$WORK/locker.conf.bak"
printf 'idle_timeout_s = 7200\n' > "$LOCKER_CONF"
as_admin systemctl --user restart qdlocker.service > /dev/null 2>&1
# scoped to the CURRENT compositor invocation — a boot's journal can hold
# an earlier locked_changed=1 from a previous session.
comp_since=$(as_admin systemctl --user show qdwin-compositor.service \
    -p ActiveEnterTimestamp --value 2>/dev/null)
last_lock=$(journalctl _SYSTEMD_USER_UNIT=qdwin-compositor.service \
    --no-pager -o cat --since "${comp_since:-1 hour ago}" 2>/dev/null \
    | sed -n 's/.*locked_changed=\([01]\).*/\1/p' | tail -1)
is "compositor not locked (focus injection requires an unlocked session)" \
    "${last_lock:-0}" "0"
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
# Model A: provision qt3s-<silo> + per-silo image store for both silos
for s in $SA $SB; do
    if ensure_silo_image "$s" weston-terminal; then pass "$s: qt3s-$s provisioned; image in its store"
    else fail "$s: ensure_silo_image failed"; fi
done
# no clipboard rules yet: default-deny
is "no test clipboard rule present" "$(yes_no test -e /etc/qdistro/rules.d/$CLIP_FILE)" no

# enforce BEFORE the launches: the restart wipes the in-memory record
# store, so the spawn's own RegisterLaunch below lands in the ENFORCING
# store — the spawn-created registration the gate then relies on.
sed -i '/^lineage_enforce/d' "$BROKER_CONF" 2>/dev/null
echo "lineage_enforce = true" >> "$BROKER_CONF"
JBR=$(journal_cursor)
systemctl restart qdistro-admin-broker.service
wait_for 30 bash -c "busctl --system list --no-pager 2>/dev/null | grep -q '^org\.qdistro\.AdminBroker1 '"
is "broker restarted under lineage_enforce" \
    "$(broker_log "$JBR" | grep -c 'lineage_enforce=True')" 1
# Receive-time lineage requires RESOLVED silo-security snapshots for both
# endpoints under enforce (the shipped registry intentionally declares no
# silos — fail-closed). Declare the test silos with empty profiles
# (resolved-clean, no guards to interfere with the rule verdict) and
# restore the file byte-exactly at the end. The TOML authority reloads
# per call — no broker restart needed.
cp "$SILO_SEC" "$WORK/silo-security.toml.bak"
printf '\n[silo.%s]\nguards = []\ncompartments = []\nconflict_classes = []\n\n[silo.%s]\nguards = []\ncompartments = []\nconflict_classes = []\n' \
    "$SA" "$SB" >> "$SILO_SEC"
is "silo-security registry stays root-owned 0644 after the test entries" \
    "$(stat -c '%a:%u:%g' "$SILO_SEC")" "644:0:0"
# the clip sources emit on connect: give each a pre-connect window for
# register_clip_source (delay lives inside the source's own exe so the
# launch record's exe axis still matches at resolve time).
export QDISTRO_CLIP_SRC_DELAY_MS=500

step "1. two GUI silos up; focus B's toplevel"
TA=$(up_gui_silo "$SA"); TB=$(up_gui_silo "$SB")
[ -n "$TA" ] && [ -n "$TB" ] && pass "both GUI launches up ($TA, $TB)" \
    || fail "launches did not come up"
# A's bridge registration was created by the spawn under enforcement;
# the probes below relay it where an attested source is required.
BP_A=$(rec "$TA" bridge_client_pid); BS_A=$(rec "$TA" bridge_client_starttime)
is "A's spawn-registered bridge pid+starttime captured" \
    "$(yes_no test -n "$BP_A" -a -n "$BS_A")" yes
HA=$(qs_ipc tier3focus findSiloHandle "$SA" | sed -n 's/^HANDLE=//p')
HB=$(qs_ipc tier3focus findSiloHandle "$SB" | sed -n 's/^HANDLE=//p')
is "findSiloHandle $SA" "${HA:--1}" "$(t3s_window_handle "$SA" "$J0")"
is "findSiloHandle $SB" "${HB:--1}" "$(t3s_window_handle "$SB" "$J0")"
is "injectFocus onto $SB's toplevel" \
    "$(qs_ipc tier3focus injectFocus "$HB" default | head -1)" "ok handle=$HB seat=default"
# the gate keys dst_silo off the focused handle — wait until the compositor
# emitted the seat_focus_changed the shell consumed before offering a
# selection, so dst_silo=$SB is deterministic.
wait_for 30 bash -c "comp_log \"\$1\" | grep -q 'seat_focus_changed seat=default handle=$HB'" _ "$J0" \
    && pass "seat focus landed on $SB's toplevel (handle $HB)" \
    || fail "no seat_focus_changed for handle $HB"

clip_gate_line() {   # clip_gate_line <src_silo> — newest CLIPBOARD_GATE for it, this run only
    qdshell_log "$J0" | grep "CLIPBOARD_GATE .*src_silo=$1 " | tail -1
}

step "2. live tagged selection $SA -> $SB: default deny"
# --emit-interval keeps A re-offering across transient broker-unavailable
# denies (busctl --timeout=200ms under load) until a broker-evaluated
# verdict lands — the same cadence B/C use below.
launch_clip_source "$SA" text/plain "s127-secret-A" "$WORK/clip-src-A.log" \
    --emit-interval 400
register_clip_launch "$SA" \
    && pass "clip source A registered in the launch-record store" \
    || fail "register_clip_source A failed"
# the emit is periodic; early offers can land while the broker name is still
# settling after the enforce restart (deny reason=broker-unavailable — still
# fail-closed). Wait for a broker-EVALUATED deny so the verdict, not the
# transient transport failure, is what gets asserted.
wait_for 30 bash -c "qdshell_log \"\$1\" | grep -q 'CLIPBOARD_GATE .*src_silo=$SA .*verdict=deny reason=broker:deny'" _ "$J0"
line=$(qdshell_log "$J0" | grep "CLIPBOARD_GATE .*src_silo=$SA .*verdict=deny reason=broker:deny" | tail -1)
info "gate: $line"
is "qdshell gate line names the real tagged source silo" \
    "$(printf '%s' "$line" | grep -c "src_silo=$SA dst_silo=$SB")" 1
is "default-deny verdict at set-time" \
    "$(printf '%s' "$line" | grep -c 'verdict=deny reason=broker:deny')" 1
is "text/plain offer reached the broker unfiltered" \
    "$(printf '%s' "$line" | grep -c 'mime_types=text/plain')" 1
# A's step-2 evidence is captured; stop its re-offers so they cannot emit
# s127a->s127b ALLOW lines once the step-4 SaveRule lands.
kill_clip_src "$CLIP_SRC_PID"; CLIP_SRC_PID=""

step "3. strict MIME: image/png-only offer strips to deny"
# under enforce a pid-less relay hard-denies; relay A's spawn-registered
# bridge (pid,starttime) for the rule-engine verdict instead.
is "broker probe still denies transfer $SA->$SB (attested bridge pid)" \
    "$(broker_check_clip "$SA" "$SB" "qdistro.tier3s.$SA" "$GUI_ENGINE" "$BP_A" "$BS_A")" deny
is "enforce: an unrecorded pid can only deny" \
    "$(broker_check_clip "$SA" "$SB" "qdistro.tier3s.$SA" "$GUI_ENGINE" 1 "$(starttime 1)")" deny
launch_clip_source "$SA" image/png "s127-png" "$WORK/clip-src-png.log"
PNG_PID=$CLIP_SRC_PID
register_clip_launch "$SA" \
    && pass "png clip source registered in the launch-record store" \
    || fail "register_clip_source png failed"
wait_for 30 bash -c "qdshell_log \"\$1\" | grep -q 'verdict=deny reason=tier3s-no-allowed-mimes'" _ "$J0"
# the source helper sweeps serials to beat weston's stale-serial guard, so one
# logical offer can emit >1 selection_set; the asserted property is the verdict
# reason, not the wire-event count.
is "qdshell denied a png-only tier3s offer before the broker" \
    "$(qdshell_log "$J0" | grep -c 'CLIPBOARD_GATE .*verdict=deny reason=tier3s-no-allowed-mimes' | awk '{print ($1>=1)?1:0}')" 1
is "qdshell logged the tier3s mime-strip" \
    "$(qdshell_log "$J0" | grep -c 'tier3s mime-strip' | awk '{print ($1>=1)?1:0}')" 1
kill_clip_src "$PNG_PID"

step "4. SaveRule flips the live verdict to allow"
RULE_BODY="- name: s127-clip-allow
  decision: allow
  match:
    action: qdistro.clipboard.transfer:$SA:$SB"
is "SaveRule wrote the file" \
    "$(save_rule "$CLIP_FILE" "$RULE_BODY")" "/etc/qdistro/rules.d/$CLIP_FILE"
is "broker logged the saverule reload" \
    "$(broker_log "$J0" | grep -c 'rules reloaded (dbus-saverule)')" 1
wait_for 20 bash -c "[ \"\$(dbus-send --system --print-reply --dest=org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 org.qdistro.AdminBroker1.CheckClipboardTransfer 'string:$SA' 'string:$SB' array:string:'text/plain' 'string:qdistro.tier3s.$SA' 'string:probe-dst' 'string:$GUI_ENGINE' boolean:false 'uint32:$BP_A' 'uint64:$BS_A' 2>/dev/null | grep -oE 'string \"[a-z]+\"' | tail -1)\" = 'string \"allow\"' ]"
is "broker probe allows transfer $SA->$SB under the rule (attested bridge pid)" \
    "$(broker_check_clip "$SA" "$SB" "qdistro.tier3s.$SA" "$GUI_ENGINE" "$BP_A" "$BS_A")" allow
# Forgery cannot steer the decision: under enforce a VERIFIED relayed pid
# overrides the claimed (silo, app, engine) with the launcher-attested
# tuple, so claiming "forged-silo" while relaying A's bridge decides the
# real A->B action (allow under the rule) and the override is journaled.
is "enforce: a forged source-silo claim cannot steer — attested s127a decides the real A->B action" \
    "$(broker_check_clip "forged-silo" "$SB" "qdistro.tier3s.forged" "$GUI_ENGINE" "$BP_A" "$BS_A")" allow
is "broker journaled the claim-vs-attested override" \
    "$(broker_log "$J0" | grep -c 'lineage ENFORCE (clipboard.transfer): source pid='"$BP_A"' .*overridden with attested')" 1
# ...but a forged destination has no rule to satisfy — default-deny holds.
is "enforce: a forged destination silo still denies (no rule for A->forged)" \
    "$(broker_check_clip "$SA" "forged-dst" "qdistro.tier3s.$SA" "$GUI_ENGINE" "$BP_A" "$BS_A")" deny

step "4a. live rule-driven allow: cross-silo $SA -> $SB (source-peer relay)"
# Sol-r2: the v35 selection_set_source_peer_identity sidecar carries the
# SOURCE wl_client's own compositor-observed (pid, starttime), so the gate
# relays the real source identity even when the source owns no focused
# toplevel. With the step-4 rule in place, a tagged A-source emitting
# while B's toplevel holds focus must reach a LIVE verdict=allow — the
# rule-driven cross-silo broker:allow the tag-equality relay could never
# prove (it relayed pid 0/0 for an unbound source and hard-denied).
J4A=$(journal_cursor)
qs_ipc tier3focus injectFocus "$HB" default > /dev/null
wait_for 30 bash -c "comp_log \"\$1\" | grep -q 'seat_focus_changed seat=default handle=$HB'" _ "$J4A" \
    && pass "seat focus on $SB's toplevel for the cross-silo offer" \
    || fail "no seat_focus_changed for handle $HB"
launch_clip_source "$SA" text/plain "s127-live-xfer" "$WORK/clip-src-X.log" \
    --emit-interval 400
XSRC_PID=$CLIP_SRC_PID
CLIP_CPID=$(read_launch_record "$CLIP_LR" "$CLIP_LR_TOK") \
    && pass "cross-silo source's real client pid resolved ($CLIP_CPID)" \
    || fail "no launch record for the cross-silo source"
register_clip_launch "$SA" \
    && pass "cross-silo clip source A registered in the launch-record store" \
    || fail "register_clip_source A failed"
# v35 evidence: the compositor emitted the SOURCE's own pid on the wire —
# not the focused toplevel's. This relay is what the allow is built on.
wait_for 30 bash -c "comp_log \"\$1\" | grep -q 'selection_set_source_peer_identity pid=$CLIP_CPID'" _ "$J4A" \
    && pass "compositor relayed the source's own pid ($CLIP_CPID)" \
    || fail "no selection_set_source_peer_identity for pid $CLIP_CPID"
# The emit interval re-offers every 400 ms, so a transient broker
# unavailability cannot starve the wait; the allow is still earned —
# deny lines from the same source may precede it (pre-registration
# emits), but the asserted property is a real allow ever landing.
wait_for 40 bash -c "qdshell_log \"\$1\" | grep -q 'CLIPBOARD_GATE .*src_silo=$SA .*dst_silo=$SB .*verdict=allow'" _ "$J4A" \
    || fail "no live cross-silo allow for $SA->$SB"
xline=$(qdshell_log "$J4A" | grep 'CLIPBOARD_GATE .*src_silo='"$SA"' .*dst_silo='"$SB"' .*verdict=allow' | tail -1)
info "gate: $xline"
is "live rule-driven allow: cross-silo $SA -> $SB (source-peer relay)" \
    "$(printf '%s' "$xline" | grep -c 'reason=broker:allow')" 1
kill_clip_src "$XSRC_PID"; CLIP_SRC_PID=""

step "4b. live attested allow: bound tagged source -> same-silo"
# Same-silo is the other live allow shape: a tagged clip source that owns
# a real xdg_toplevel in its own silo s127c, focused, registered,
# verified — the exact trust shape a tier3s bridge takes when its own
# window holds focus. The v35 sidecar relays the source's own pid here
# too (it is the same client as the toplevel's), and VerifyClientIdentity
# gates the same-silo shortcut.
QDISTRO_CLIP_SRC_DELAY_MS=800 \
    launch_clip_source "$SC" text/plain "s127-own-clip" "$WORK/clip-src-C.log" \
        --toplevel --title "clipsrc-$SC" --emit-interval 400
register_clip_launch "$SC" \
    && pass "bound clip source C registered in the launch-record store" \
    || fail "register_clip_source C failed"
wait_for 30 bash -c "qdshell_log \"\$1\" | grep -q '\\[tier3s\\] toplevel observed silo=$SC '" _ "$J0" \
    && pass "qdshell observed the bound source's toplevel" \
    || fail "no [tier3s] toplevel observed for $SC"
HC=$(qs_ipc tier3focus findSiloHandle "$SC" | sed -n 's/^HANDLE=//p')
is "findSiloHandle $SC" "${HC:--1}" "$(t3s_window_handle "$SC" "$J0")"
# the handle's attested tag must be complete BEFORE the first bound emit
# evaluates — wait for the compositor's security_context+peer_identity
# pair for this handle (the sidecar needs the same tuple to bind).
wait_for 30 bash -c "comp_log \"\$1\" | grep -q 'toplevel_security_context handle=$HC engine=qdistro.tier3s app_id=qdistro.tier3s.$SC instance=clipsrc-$SC-$CLIP_SRC_PID'" _ "$J0" \
    && pass "compositor attested the bound source's tag on its toplevel" \
    || fail "no toplevel_security_context for handle $HC"
is "injectFocus onto the bound source's toplevel" \
    "$(qs_ipc tier3focus injectFocus "$HC" default | head -1)" "ok handle=$HC seat=default"
wait_for 30 bash -c "comp_log \"\$1\" | grep -q 'seat_focus_changed seat=default handle=$HC'" _ "$J0" \
    && pass "seat focus landed on the bound source (handle $HC)" \
    || fail "no seat_focus_changed for handle $HC"
# The emit interval re-offers every 400 ms: the first bound offer under
# a cold VerifyClientIdentity cache still denies (the same-silo shortcut
# needs identity_verified); the allow can only appear AFTER the broker
# attested the relayed pid — an allow here is earned, never a default.
wait_for 30 bash -c "qdshell_log \"\$1\" | grep -q 'CLIPBOARD_GATE .*src_silo=$SC .*verdict=allow'" _ "$J0" \
    || fail "bound tagged source never reached a live attested allow"
# Assert on the allow the wait saw, not the newest line: the source keeps
# re-offering every 400 ms and a later offer can still hit a transient
# broker-unavailable deny (busctl --timeout=200ms under load).
line=$(qdshell_log "$J0" | grep "CLIPBOARD_GATE .*src_silo=$SC .*verdict=allow" | head -1); info "gate: $line"
is "live attested allow: bound tagged source -> same-silo (verify + record resolution)" \
    "$(printf '%s' "$line" | grep -c 'verdict=allow')" 1
is "cold-verify bound offers denied first (the allow was earned)" \
    "$(qdshell_log "$J0" | grep -c "CLIPBOARD_GATE .*src_silo=$SC .*verdict=deny" | awk '{print ($1>=1)?1:0}')" 1
is "live allow carries dst_silo=$SC (bound => focused => same-silo)" \
    "$(printf '%s' "$line" | grep -c "dst_silo=$SC")" 1
kill_clip_src "$CLIP_SRC_PID"; CLIP_SRC_PID=""

step "5. focus-aware clear: selection owned by $SB, focus crosses to $SA"
# Re-focus B and set a B-tagged (denied-but-recorded) selection so
# _selectionSourceSilo tracks B; then move focus to A's toplevel — the
# cross-silo transition must clear and journal CLIPBOARD_FOCUS_GATE.
# J5 scopes every wait/assert below to THIS step's fresh events: an
# earlier emit while B was focused also records src_silo=$SB, so a
# J0-scoped gate-line wait can match a stale line and let injectFocus
# race ahead of B's real offer (leaving the record empty at the focus
# event — reproduced on the preserved VM).
J5=$(journal_cursor)
qs_ipc tier3focus injectFocus "$HB" default > /dev/null
wait_for 30 bash -c "comp_log \"\$1\" | grep -q 'seat_focus_changed seat=default handle=$HB'" _ "$J5" \
    && pass "seat focus re-landed on $SB's toplevel (handle $HB)" \
    || fail "no seat_focus_changed for handle $HB"
# --emit-interval keeps B alive across the set-time deny's clearSelection
# (cancel->re-offer): the FOCUS_GATE verdict is the act under test —
# focus crossing while B still holds the selection.
launch_clip_source "$SB" text/plain "s127-secret-B" "$WORK/clip-src-B.log" \
    --emit-interval 400
BSRC_PID=$CLIP_SRC_PID
register_clip_launch "$SB" \
    && pass "clip source B registered in the launch-record store" \
    || fail "register_clip_source B failed"
wait_for 30 bash -c "qdshell_log \"\$1\" | grep -q 'CLIPBOARD_GATE .*src_silo=$SB '" _ "$J5" \
    && pass "B's tagged offer recorded (_selectionSourceSilo tracks $SB)" \
    || fail "no post-launch CLIPBOARD_GATE for $SB"
qs_ipc tier3focus injectFocus "$HA" default > /dev/null
wait_for 30 bash -c "qdshell_log \"\$1\" | grep -q 'CLIPBOARD_FOCUS_GATE .*src_silo=$SB .*dst_silo=$SA'" _ "$J5"
kill_clip_src "$BSRC_PID"
is "focus crossing out of the source silo cleared the selection" \
    "$(qdshell_log "$J5" | grep -c "CLIPBOARD_FOCUS_GATE .*src_silo=$SB dst_silo=$SA .*verdict=deny reason=focus-cross-silo")" 1
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
wait_for 20 bash -c "[ \"\$(dbus-send --system --print-reply --dest=org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 org.qdistro.AdminBroker1.CheckClipboardReceive 'string:$SA' 'string:$SB' 'string:text/plain' 'string:qdistro.tier3s.$SA' 'string:probe-dst' 'string:$GUI_ENGINE' boolean:false 'uint32:$BP_A' 'uint64:$BS_A' 2>/dev/null | grep -oE 'string \"[a-z]+\"' | tail -1)\" = 'string \"allow\"' ]"
is "receive probe allows text/plain under the mime rule (attested bridge pid)" \
    "$(broker_check_clip_recv "$SA" "$SB" text/plain "qdistro.tier3s.$SA" "$GUI_ENGINE" "$BP_A" "$BS_A")" allow
is "receive probe still denies image/png (mime selector)" \
    "$(broker_check_clip_recv "$SA" "$SB" image/png "qdistro.tier3s.$SA" "$GUI_ENGINE" "$BP_A" "$BS_A")" deny

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
# restore the broker's pre-test lineage posture (exact prior line or its
# absence) and restart so the running broker matches what it was before.
sed -i '/^lineage_enforce/d' "$BROKER_CONF" 2>/dev/null
[ -n "$PRIOR_ENFORCE_LINE" ] && echo "$PRIOR_ENFORCE_LINE" >> "$BROKER_CONF"
systemctl restart qdistro-admin-broker.service
wait_for 30 bash -c "busctl --system list --no-pager 2>/dev/null | grep -q '^org\.qdistro\.AdminBroker1 '"
is "lineage_enforce restored (prior: '${PRIOR_ENFORCE_LINE:-absent}')" \
    "$(broker_log | sed -n 's/.*lineage_enforce=\([A-Za-z]*\).*/\1/p' | tail -1)" \
    "$([ -n "$PRIOR_ENFORCE_LINE" ] && echo True || echo False)"
# restore the registry to its exact prior bytes (the exit hook repeats
# this harmlessly if the driver dies before reaching it)
cp "$WORK/silo-security.toml.bak" "$SILO_SEC"
is "silo-security registry restored byte-exact" \
    "$(sha256sum "$SILO_SEC" | cut -d' ' -f1)" "$PRIOR_SILO_SEC_SHA"
if [ -f "$WORK/locker.conf.bak" ]; then
    cp "$WORK/locker.conf.bak" "$LOCKER_CONF"
else
    rm -f "$LOCKER_CONF"
fi
# ABSENT sentinel: an empty sha is "nothing to compare" for the assert,
# so absent->absent must be spelled out rather than falling out as ''.
is "locker idle-timeout restored" \
    "$([ -f "$LOCKER_CONF" ] && sha256sum "$LOCKER_CONF" | cut -d' ' -f1 || echo ABSENT)" \
    "${PRIOR_LOCKER_CONF_SHA:-ABSENT}"
for s in $SA $SB; do
    sm StopSilo si "$s" 10 > /dev/null
    is "StopSilo $s" "$(silo_state "$s")" Stopped
    wait_for 90 unit_down "$(unit_of "$s")"
done
assert_bridge_gone "cleanup/A" "$TA"; assert_launch_gone "cleanup/A" "$TA" "$SA"
assert_bridge_gone "cleanup/B" "$TB"; assert_launch_gone "cleanup/B" "$TB" "$SB"
for s in $SA $SB; do sm DeleteSilo s "$s" > /dev/null; is "DeleteSilo $s" "$(silo_state "$s")" absent; done
set_rules none
assert_all_clear end
finish
