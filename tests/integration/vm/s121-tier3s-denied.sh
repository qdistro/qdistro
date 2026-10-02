#!/bin/bash
# s121-tier3s-denied.sh — GUEST driver (root) for phase7-tier3s-denied.bats.
# Tier 3s refusals, todo/paravirt 06 "Δ DONE bar":
#   item 5  broker denial (no rule = unknown, and an explicit deny rule) =>
#           no `podman run` and no activation record. Oracles: admin's podman
#           event stream has no container event, systemd never started an
#           owning scope, no control record or per-launch dir, no container,
#           and for a TEMPLATED silo no activation record
#           (/run/qdistro/silo-generation/<silo>, bindings/<silo>.activated).
#           A positive control (allow) shows both oracles DO see a launch.
#   item 6  a non-dev (release = the hardened config) profile refuses with a
#           clear message at CreateTier3sSilo, at StartSilo and in the spawn
#           itself (direct unit start); a probe failure refuses; no fallback
#           to tier 2/3 anywhere (no tier-2/podapp/tier-3 session unit starts,
#           no container of any kind appears).
# The templated silo uses a binding FIXTURE written with the installed
# qdistro_templates API (generation = the loaded tier3s image's ID), so the
# activation-record oracle is not vacuous: an untemplated silo never writes one.
# Runs after tier3s-guest-setup.sh. One PASS/FAIL line per check;
# `[s121] N passes, M failures`; exit 1 on any failure.
set -u
T3S_TAG=s121
. "$(dirname "$0")/tier3s-guest-lib.sh"
SA=s121a; ST=s121t
FIX_TEMPLATE=t3sfixture
FIX_STATE_PARENT=/var/tmp/t3s-s121-state
GEN_STATUS=/run/qdistro/silo-generation/$ST
MARKER=/var/lib/qdistro/bindings/$ST.activated
PROFILE=/etc/qdistro/profile

step "0. preconditions, silos, templated-silo fixture"
is "probe PASS" "$(/usr/lib/qdistro/tier3s/probe.sh --user admin > /dev/null 2>&1; echo $?)" 0
is "image present" "$(yes_no pm image exists "$IMAGE")" yes
is "profile is dev" "$(sed -n 's/^QDISTRO_PROFILE=//p' "$PROFILE" | tail -1)" dev
assert_all_clear pre
GEN="sha256:$(pm image inspect --format '{{.Id}}' "$IMAGE")"
install -d -m 0755 "$FIX_STATE_PARENT"
install -d -o 1000 -g 1000 -m 0700 "$FIX_STATE_PARENT/state" "$FIX_STATE_PARENT/state/.cache"
as_admin env PYTHONPATH=/usr/libexec/qdistro python3 - "$ST" "$FIX_TEMPLATE" "$GEN" "$FIX_STATE_PARENT/state" <<'PY'
# test FIXTURE: a promoted-generation record + binding for the loaded tier3s
# image, written through the real qdistro_templates API (as admin, like the
# template tools), so qdistro-resolve-binding resolves the silo as templated.
import os, sys
import qdistro_templates as qt
silo, template, gen, state = sys.argv[1:5]
L = qt.Layout()
gd = L.generation_dir(template, gen)
os.makedirs(gd, exist_ok=True)
qt.write_toml_atomic(os.path.join(gd, "manifest.toml"), qt.validate_manifest({
    "template": template, "run_id": "s121-fixture", "image_digest": gen, "image_id": gen,
    "containerfile_digest": gen, "build_command": "tier3s/cache-image-archive.sh (s121 fixture)",
    "network_mode": "none", "artifact_manifest": [], "generation_ref": gen}), 0o644)
qt.write_binding(L.binding_file(silo), {
    "silo": silo, "template": template, "backend": "podman-image", "active_generation": gen,
    "state_path": state, "activation_policy": "manual", "identity_revision": 0})
print(f"fixture: binding {L.binding_file(silo)} -> {gen}")
PY
out=$(as_admin /usr/bin/python3 /usr/libexec/qdistro/qdistro_resolve_binding.py "$ST" --launch-env 2>&1); rc=$?
is "fixture: $ST resolves as templated (read-only resolution)" "$rc:$(printf '%s\n' "$out" | sed -n 's/^GENERATION=//p')" "0:$GEN"
no_activation() { [ ! -e "$GEN_STATUS" ] && [ ! -e "$MARKER" ]; }
is "fixture: no activation record yet" "$(yes_no no_activation)" yes
for s in $SA $ST; do
    sm CreateTier3sSilo ssss "$s" headless-smoke "$s" none > /dev/null; is "CreateTier3sSilo $s" "$(silo_state "$s")" Created
done
set_argv "$SA=default" "$ST=default" | sed 's/^/    /'

# One refused launch and every "nothing happened" oracle. refused <tag> <silo>
# <REFUSE substring> [start-cmd...]: default start = StartSilo through the manager.
refused() {
    local tag="$1" silo="$2" want="$3" unit cur t0 rc n
    shift 3
    unit=$(unit_of "$silo"); cur=$(journal_cursor); t0=$(date --iso-8601=seconds)
    sleep 1
    if [ "$#" -gt 0 ]; then "$@" > "$WORK/start.out" 2>&1; rc=$?
    else sm StartSilo s "$silo" > "$WORK/start.out" 2>&1; rc=$?; fi
    info "$tag: start rc=$rc $(tr '\n' ' ' < "$WORK/start.out" | cut -c1-200)"
    wait_for 60 unit_down "$unit"
    sleep 1
    unit_log "$unit" "$cur" | grep -v pam_unix | sed 's/^/    unit: /'
    is "$tag: the spawn refused with the expected message" "$(unit_log "$unit" "$cur" | grep -cF "spawn-tier3s: REFUSE: $want")" 1
    is "$tag: refusal fails the launch unit visibly (exit 2)" \
        "$(systemctl show -p Result --value "$unit"):$(systemctl show -p ExecMainStatus --value "$unit")" "exit-code:2"
    launch_events_since "$t0" | sed 's/^/    podman event: /'
    is "$tag: no podman run (no container event but the probe's scratch create/remove)" "$(launch_events_since "$t0" | grep -c .)" 0
    is "$tag: systemd never started an owning scope" "$(units_started_since "$cur" "$T3S_SCOPE_RE")" 0
    is "$tag: no control record, no per-launch dir" "$(records | wc -l):$(find "$LAUNCHES" -mindepth 1 | wc -l)" "0:0"
    is "$tag: no container of any kind" "$(pm ps -a --format '{{.Names}}' | grep -c .)" 0
    if [ "$silo" = "$ST" ]; then
        is "$tag: no activation record for the templated silo" "$(yes_no no_activation)" yes
    fi
    is "$tag: no fallback: no tier-2/podapp/tier-3 session unit was started" "$(units_started_since "$cur" "$FALLBACK_RE")" 0
    info "$tag: silo state afterwards: $(silo_state "$silo")"
    sm StopSilo si "$silo" 10 > /dev/null 2>&1
    systemctl reset-failed "$unit" 2>/dev/null
}
GATE_UNKNOWN="broker has no allow rule for headless-smoke/$SMOKE_APP (action='$ACTION' decision=unknown)"
GATE_DENY="broker denied headless-smoke/$SMOKE_APP (action='$ACTION' decision=deny)"

step "1. DONE 5: broker has no rule (rules-only prefix => unknown) => refused"
set_rule none; is "broker answers unknown" "$(broker_check "$ACTION")" unknown
refused "no-rule/untemplated" $SA "$GATE_UNKNOWN"
refused "no-rule/templated" $ST "$GATE_UNKNOWN"

step "2. DONE 5: explicit deny rule => refused"
set_rule deny; is "broker answers deny" "$(broker_check "$ACTION")" deny
refused "deny/untemplated" $SA "$GATE_DENY"
refused "deny/templated" $ST "$GATE_DENY"

step "3. positive control: allow => both oracles see the launch"
set_rule allow; is "broker answers allow" "$(broker_check "$ACTION")" allow
cur=$(journal_cursor); t0=$(date --iso-8601=seconds); sleep 1
sm StartSilo s $ST > /dev/null; is "control: StartSilo $ST rc" "$?" 0
wait_for 90 unit_down "$(unit_of $ST)"
unit_log "$(unit_of $ST)" "$cur" | grep -v pam_unix | sed 's/^/    unit: /'
is "control: activation status written (generation = fixture)" "$(sed -n "s/^generation = '\(.*\)'$/\1/p" "$GEN_STATUS" 2>/dev/null)" "$GEN"
is "control: activation marker committed" "$(tr -d ' \n' < "$MARKER" 2>/dev/null)" "$GEN"
launch_events_since "$t0" | sed 's/^/    podman event: /'
is "control: the same event oracle sees the container start" "$(launch_events_since "$t0" | grep -c "^start qdistro-tier3s-$ST\$")" 1
is "control: the same scope oracle sees the owning scope start" "$(units_started_since "$cur" "$T3S_SCOPE_RE")" 1
tok=$(unit_log "$(unit_of $ST)" "$cur" | sed -n 's/^LAUNCH_TOKEN=\([0-9a-f]\{32\}\)$/\1/p' | head -1)
is "control: templated launch ran the image by its generation digest" "$(unit_log "$(unit_of $ST)" "$cur" | grep -c "^IMAGE=$GEN\$")" 1
is "control: the smoke ran to its end under gVisor" "$(scope_log "$tok" | grep -c '^SMOKE done' | sed 's/[1-9][0-9]*/yes/')" yes
is "control: launch unit Result" "$(systemctl show -p Result --value "$(unit_of $ST)")" success
sm StopSilo si $ST 10 > /dev/null; is "control: StopSilo" "$(silo_state $ST)" Stopped
assert_all_clear control

step "4. DONE 6: release (hardened config) profile refuses with a clear message"
cp -a "$PROFILE" "$WORK/profile.orig"
printf 'QDISTRO_PROFILE=release\n' > "$PROFILE"; chmod 0644 "$PROFILE"
MSG="tier 3s is dev-profile only in this PoC (profile=release); there is no hardened launch path and no fallback tier"
out=$(sm CreateTier3sSilo ssss s121h headless-smoke s121h none 2>&1); rc=$?
info "CreateTier3sSilo on release: rc=$rc $out"
is "release: CreateTier3sSilo refused with the message" "$([ "$rc" -ne 0 ] && printf '%s' "$out" | grep -cF "$MSG")" 1
is "release: no silo was created" "$(silo_state s121h)" absent
cur=$(journal_cursor)
out=$(sm StartSilo s $SA 2>&1); rc=$?
info "StartSilo on release: rc=$rc $out"
is "release: StartSilo refused with the message" "$([ "$rc" -ne 0 ] && printf '%s' "$out" | grep -cF "$MSG")" 1
is "release: StartSilo started no launch unit" "$(units_started_since "$cur" 'qdistro-tier3s-silo@.*')" 0
is "release: silo state unchanged" "$(silo_state $SA)" Stopped
# the spawn refuses on its own: a hand-written stanza, the unit started directly
start_direct() { write_stanza $SA "[\"$SMOKE_APP\"]" > /dev/null; systemctl start "$(unit_of $SA)"; }
refused "release/spawn (direct unit start)" $SA \
    "tier 3s is dev-profile only in this PoC (QDISTRO_PROFILE=release); there is no hardened launch path and no fallback tier" start_direct
rm -f "/run/qdistro/silo-launch/$SA.env"
out=$(/usr/lib/qdistro/tier3s/probe.sh --user admin 2>&1); rc=$?
is "release: the probe refuses a non-dev profile (rc 2)" "$rc" 2
cp -a "$WORK/profile.orig" "$PROFILE"
is "profile restored to dev" "$(sed -n 's/^QDISTRO_PROFILE=//p' "$PROFILE" | tail -1)" dev

step "5. DONE 6: a probe failure refuses (runtime wrapper missing), no fallback"
mv "$WRAPPER" "$WRAPPER.s121-aside"
out=$(/usr/lib/qdistro/tier3s/probe.sh --user admin 2>&1); rc=$?
is "probe fails with the wrapper missing" "$rc:$(printf '%s\n' "$out" | grep -c '^RESULT FAIL: first missing prerequisite: wrapper')" "1:1"
refused "probe-failure" $SA "probe failed (rc=1): RESULT FAIL: first missing prerequisite: wrapper"
mv "$WRAPPER.s121-aside" "$WRAPPER"
is "probe PASS again with the wrapper restored" "$(/usr/lib/qdistro/tier3s/probe.sh --user admin > /dev/null 2>&1; echo $?)" 0

step "6. cleanup"
for s in $SA $ST; do sm DeleteSilo s "$s" > /dev/null; is "DeleteSilo $s" "$(silo_state "$s")" absent; done
as_admin env PYTHONPATH=/usr/libexec/qdistro python3 - "$ST" "$FIX_TEMPLATE" <<'PY'
import os, shutil, sys
import qdistro_templates as qt
silo, template = sys.argv[1:3]
L = qt.Layout()
for f in (L.binding_file(silo), os.path.join(L.bindings_dir, f"{silo}.activated")):
    if os.path.exists(f): os.unlink(f)
shutil.rmtree(L.template_dir(template), ignore_errors=True)
PY
rm -rf "$FIX_STATE_PARENT" "$GEN_STATUS"
is "fixture removed" "$(yes_no test -e "/var/lib/qdistro/bindings/$ST.toml")" no
assert_all_clear end
finish
