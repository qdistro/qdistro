#!/bin/bash
# dev-prov.sh — GUEST (root), dev VM only. astra+fable A r3 P1 on real
# systemd/podman/runuser: the `podman container exists` verdict is the
# in-call PMRC=<rc> line the dropped-privilege command itself prints, never
# the timeout->systemd-run->runuser chain's own status. Fixtures are REAL
# launches (CreateTier3sSilo + StartSilo, argv --hold), so the control
# record, the scope and the labelled container are all real.
#
#   0  preconditions; two tier3s silos created, argv --hold
#   1  launch A live (record phase=running, scope active, container running)
#   2  cleanup under a fake systemd-run that exits 1 without exec ->
#      nonzero rc, "NOT treating it as absent", launch/record/container kept
#   3  same under a fake runuser
#   4  same under a fake podman whose noise shares the PMRC line's output
#   5  the real cleanup of launch A: PMRC=0 -> inspect/stop/rm -> PMRC=1,
#      everything torn down, rc 0
#   6  launch B; its container removed directly as admin; cleanup takes the
#      genuine PMRC=1 path and still tears scope+record down, rc 0
#   7  the session manager's PMRC verdict end-to-end on the real
#      runuser->env->sh->podman chain (installed file, real subprocesses):
#      present -> 0, absent -> 1, fake runuser / garbage stdout -> None
#   8  silos deleted, all clear
set -u
T3S_TAG=devprov
. "$(dirname "$0")/tier3s-guest-lib.sh"
D=/var/tmp/t3s-prov
rm -rf "$D"; install -d -m 0755 "$D"

# fake_over <fake-file> <target> <cmd...>: cmd with <fake-file> bind-mounted
# over <target> in a PRIVATE mount namespace. systemd-run execs the payload
# in its caller's mount ns, so the fake reaches the whole chain; nothing
# else on the VM sees it.
fake_over() {
    unshare -m --propagation private bash -c \
        'mount --bind "$1" "$2" && shift 2 && exec "$@"' _ "$1" "$2" "${@:3}"
}
# still_live <tag> <token> <silo>: what a preserved teardown must look like
still_live() {  # still_live <tag> <token> <silo>
    local tag="$1" tok="$2" s="$3"
    is "$tag: control record preserved" "$(yes_no test -f "$CTL/$tok/state")" yes
    is "$tag: record still phase=running" "$(rec "$tok" phase)" running
    is "$tag: scope still active" "$(unit_state "qdistro-tier3s-$tok.scope")" active
    is "$tag: launch unit still active" "$(unit_state "$(unit_of "$s")")" active
    pm container exists "$(ctr_of "$s")"
    is "$tag: container still present (direct podman rc)" "$?" 0
}
run_cleanup() {  # run_cleanup <cmd...> -> $OUT, rc; stderr folded in
    OUT="$("$@" 2>&1)"; return $?
}

step "0. preconditions (setup ran)"
out=$(/usr/lib/qdistro/tier3s/probe.sh --user admin 2>&1); rc=$?
is "probe PASS before the launches" "$rc:$(printf '%s\n' "$out" | grep -c '^RESULT PASS')" "0:1"
is "image present" "$(yes_no pm image exists "$IMAGE")" yes
is "broker allows the smoke spawn" "$(broker_check "$ACTION")" allow
assert_all_clear pre
SA=devprova; SB=devprovb
for s in $SA $SB; do
    sm CreateTier3sSilo ssss "$s" headless-smoke "$s" none > /dev/null
    is "CreateTier3sSilo $s" "$(silo_state "$s")" Created
done
set_argv "$SA=600" "$SB=600" | sed 's/^/    /'
is "argv set with the manager restarted" "$(yes_no manager_up)" yes

step "1. launch A live"
TA=$(up_silo $SA); CA=$(ctr_of $SA); UA=$(unit_of $SA)
if [ -n "$TA" ]; then pass "launch A $TA recorded running"; else fail "launch A did not come up"; finish; fi
is "silo A Active" "$(silo_state $SA)" Active

step "2. systemd-run refuses before exec: never a verdict"
cat > "$D/systemd-run" <<'FAKE'
#!/bin/bash
echo "fake systemd-run: StartTransientUnit refused" >&2
exit 1
FAKE
chmod 0755 "$D/systemd-run"
run_cleanup fake_over "$D/systemd-run" /usr/bin/systemd-run "$CLEANUP" "$TA"; rc=$?
printf '%s\n' "$OUT" | sed 's/^/    /'
is "cleanup under a refusing systemd-run: rc" "$rc" 4
is "cleanup under a refusing systemd-run: not absent" \
    "$(printf '%s\n' "$OUT" | grep -c 'NOT treating it as absent')" 1
still_live sdr-fail "$TA" "$SA"

step "3. runuser fails before podman: never a verdict"
cat > "$D/runuser" <<'FAKE'
#!/bin/bash
echo "fake runuser: setup failure" >&2
exit 1
FAKE
chmod 0755 "$D/runuser"
RU=$(command -v runuser)
run_cleanup fake_over "$D/runuser" "$RU" "$CLEANUP" "$TA"; rc=$?
printf '%s\n' "$OUT" | sed 's/^/    /'
is "cleanup under a failing runuser: rc" "$rc" 4
is "cleanup under a failing runuser: not absent" \
    "$(printf '%s\n' "$OUT" | grep -c 'NOT treating it as absent')" 1
still_live ru-fail "$TA" "$SA"

step "4. a PMRC verdict that shares its output is no verdict"
cat > "$D/podman" <<'FAKE'
#!/bin/bash
# answers 'present', but with noise on the same stdout: the verdict line
# must be the call's WHOLE output, so this is a failed query, never 0
echo "bogus noise from the container runtime"
exit 0
FAKE
chmod 0755 "$D/podman"
run_cleanup fake_over "$D/podman" /usr/bin/podman "$CLEANUP" "$TA"; rc=$?
printf '%s\n' "$OUT" | sed 's/^/    /'
is "cleanup under a noisy podman: rc" "$rc" 4
is "cleanup under a noisy podman: query failed" \
    "$(printf '%s\n' "$OUT" | grep -c 'podman query failed')" 1
still_live noisy "$TA" "$SA"

step "5. the real chain, container present: full teardown"
run_cleanup "$CLEANUP" "$TA"; rc=$?
printf '%s\n' "$OUT" | sed 's/^/    /'
is "cleanup of live launch A: rc" "$rc" 0
is "cleanup of live launch A: torn down" \
    "$(printf '%s\n' "$OUT" | grep -c "$TA: torn down")" 1
assert_launch_gone cleanup-A "$TA" "$CA"
is "launch unit A inactive" "$(unit_state "$UA")" inactive

step "6. genuine absent (PMRC=1): a record whose container never existed"
# A synthetic record (what mkrec wrote for dev-kill): no unit runs, so the
# unit's ExecStopPost cannot race this cleanup. runsc_root is the tmpfiles
# dir the installer made; the scope and container never existed.
TB=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
install -d -m 0700 "$CTL/$TB"
( umask 077; printf '%s\n' schema=1 "token=$TB" container=qdistro-tier3s-devprovb \
    "unit=qdistro-tier3s-silo@devprovb.service" "scope_unit=qdistro-tier3s-$TB.scope" admin_uid=1000 \
    "runsc_root=/run/qdistro-tier3s-runsc/1000" "per_launch_dir=/run/qdistro-tier3s/$TB" phase=created \
    > "$CTL/$TB/state" )
is "synthetic record B passes read_record shape" "$(yes_no test -f "$CTL/$TB/state")" yes
is "the runsc root it records exists" "$(yes_no test -d "$SROOT")" yes
run_cleanup "$CLEANUP" "$TB"; rc=$?
printf '%s\n' "$OUT" | sed 's/^/    /'
is "absent-container cleanup B: rc" "$rc" 0
is "absent-container cleanup B: torn down" \
    "$(printf '%s\n' "$OUT" | grep -c "$TB: torn down")" 1
is "absent-container cleanup B: record removed" "$(yes_no test -e "$CTL/$TB")" no

step "7. the session manager's PMRC verdict on the real chain"
# a scratch container (created, never started) is enough for 'present'
pm create --name qdistro-tier3s-devprov "$IMAGE" > /dev/null
mkdir -p "$D/fakebin" "$D/fakebin2"
cat > "$D/fakebin/runuser" <<'FAKE'
#!/bin/bash
exit 1
FAKE
cat > "$D/fakebin2/runuser" <<'FAKE'
#!/bin/bash
echo "not a verdict at all"
exit 0
FAKE
chmod 0755 "$D/fakebin/runuser" "$D/fakebin2/runuser"
is "manager verdict: present" \
    "$(D=$D python3 -c 'import os,sys;sys.path.insert(0,"/usr/libexec/qdistro");from qdistro_session_manager import _SystemOps as S;print(S._tier3s_container_exists("qdistro-tier3s-devprov"))')" 0
is "manager verdict: absent" \
    "$(D=$D python3 -c 'import os,sys;sys.path.insert(0,"/usr/libexec/qdistro");from qdistro_session_manager import _SystemOps as S;print(S._tier3s_container_exists("qdistro-tier3s-nosuch0000"))')" 1
is "manager verdict: runuser failure is no verdict" \
    "$(PATH=$D/fakebin:$PATH python3 -c 'import sys;sys.path.insert(0,"/usr/libexec/qdistro");from qdistro_session_manager import _SystemOps as S;print(S._tier3s_container_exists("qdistro-tier3s-devprov"))')" None
is "manager verdict: garbage stdout is no verdict" \
    "$(PATH=$D/fakebin2:$PATH python3 -c 'import sys;sys.path.insert(0,"/usr/libexec/qdistro");from qdistro_session_manager import _SystemOps as S;print(S._tier3s_container_exists("qdistro-tier3s-devprov"))')" None
is "manager verdict (plain runuser leg): present" \
    "$(python3 -c 'import sys;sys.path.insert(0,"/usr/libexec/qdistro");from qdistro_session_manager import _SystemOps as S;print(S._podman_exists_verdict(S._runuser_exists("qdistro-tier3s-devprov")))')" 0
is "manager verdict (plain runuser leg): absent" \
    "$(python3 -c 'import sys;sys.path.insert(0,"/usr/libexec/qdistro");from qdistro_session_manager import _SystemOps as S;print(S._podman_exists_verdict(S._runuser_exists("qdistro-tier3s-nosuch0000")))')" 1
pm rm -f qdistro-tier3s-devprov > /dev/null

step "8. teardown of the fixtures"
for s in $SA $SB; do
    sm StopSilo si "$s" 10 > /dev/null
    sm DeleteSilo s "$s" > /dev/null; is "DeleteSilo $s" "$(silo_state "$s")" absent
done
assert_all_clear final
finish
