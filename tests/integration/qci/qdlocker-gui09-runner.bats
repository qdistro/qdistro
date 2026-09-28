#!/usr/bin/env bats
#
# Host-only test of qdlocker/tests/gui/09-capture-indicators.d/run.sh, the
# host side of scenario 09's guest<->host step handshake.
#
# The defect it pins (full-20260928T154720Z-3346753, ERROR after Step 7): the
# host loop served a FIXED list of step names (s1 s2 s3 s4 s7 ...). The guest
# driver skipped the conditional Step 4 (no default sink) and published `s7`;
# the host kept polling for `s4`, so s7 and every later step were never
# released and the run was recorded as a transport failure. run.sh must serve
# whatever step the guest publishes, in whatever order, with conditional
# steps absent.
#
# The "guest" here is a local bash script using the REAL qci_host_step from
# ci/lib/guest/gui-waiters.sh; the fake vm-exec runs guest commands locally.
# The host actions (screenshot, drain) are replaced through run.sh's
# QDLOCKER_09_HOOKS seam; the step protocol, the token parsing and the
# go-marker are run.sh's own code.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    D09="$REPO_ROOT/qdlocker/tests/gui/09-capture-indicators.d"
    T="$BATS_TEST_TMPDIR"
    mkdir -p "$T/bin" "$T/art"
    cat > "$T/bin/fake-vm-exec" <<'EOF'
#!/bin/bash
exec bash -c "$2"
EOF
    chmod +x "$T/bin/fake-vm-exec"
    cat > "$T/hooks.sh" <<EOF
act_capture() { echo "capture \$1 \$2" >>"$T/served"; check "\$1" capture PASS fake; }
act_drain()   { echo "drain \$1" >>"$T/served"; check "\$1" drain PASS fake; }
act_heads()   { echo "heads \$1" >>"$T/served"; check "\$1" heads PASS fake; }
EOF
    export QCI_GUI_ARTIFACT_DIR="$T/art"
    export QDLOCKER_09_VMEXEC="$T/bin/fake-vm-exec"
    export QDLOCKER_09_GUEST_DIR="$T/g"
    export QDLOCKER_09_HOOKS="$T/hooks.sh"
    export QDLOCKER_09_DRIVER_TIMEOUT=60
}

# fake_guest <verdict-line> <step>... : a guest driver that publishes exactly
# these host steps through the real qci_host_step, then prints the verdict.
fake_guest() {
    local verdict=$1; shift
    {
        echo '#!/bin/bash'
        echo "source '$REPO_ROOT/ci/lib/guest/gui-waiters.sh' || exit 2"
        echo 'export QCI_HOST_STEP_DIR=$QDLOCKER_09_DIR'
        echo "for s in $*; do qci_host_step \"\$s\" 20; echo \"ASSERT \$s PASS ok\"; done"
        echo "echo '$verdict'"
        echo "case '$verdict' in 'VERDICT PASS') exit 0;; 'VERDICT FAIL'*) exit 1;; *) exit 3;; esac"
    } > "$T/guest.sh"
    export QDLOCKER_09_GUEST_SCRIPT="$T/guest.sh"
}

@test "serves the steps the guest publishes when conditional steps 4/5/6/10 are skipped" {
    fake_guest 'VERDICT PASS' setup-drain s1-quiet s2-alarm s3-quiet s7-alarm s8a-rec s8b-alarm s9-drain s9-rec cleanup-drain
    run timeout 90 bash "$D09/run.sh" fake-vm
    [ "$status" -eq 0 ]
    [[ "$output" == *"RESULT PASS"* ]]
    run cat "$T/served"
    [ "$output" = "drain setup
capture s1 quiet
capture s2 alarm
capture s3 quiet
capture s7 alarm
capture s8a rec
capture s8b alarm
drain s9
capture s9 rec
drain cleanup" ]
}

@test "serves every conditional step too when the guest runs them" {
    fake_guest 'VERDICT PASS' setup-drain s1-quiet s4-alarm s5-alarm s6-alarm s10-drain s10-heads cleanup-drain
    run timeout 90 bash "$D09/run.sh" fake-vm
    [ "$status" -eq 0 ]
    run cat "$T/served"
    [ "$output" = "drain setup
capture s1 quiet
capture s4 alarm
capture s5 alarm
capture s6 alarm
drain s10
heads s10
drain cleanup" ]
}

@test "an unknown action is an ERROR but its go is still sent (no deadlock)" {
    fake_guest 'VERDICT PASS' s1-quiet s2-bogus s3-quiet
    run timeout 90 bash "$D09/run.sh" fake-vm
    [ "$status" -eq 3 ]
    [[ "$output" == *"RESULT ERROR"* ]]
    grep -q $'^s2-bogus\taction\tERROR' "$T/art/host-checks.tsv"
    grep -q '^ASSERT s3-quiet PASS' "$T/art/driver.log"
}

@test "a guest FAIL verdict makes the run FAIL" {
    fake_guest 'VERDICT FAIL (1 failed guest assertion(s))' s1-quiet
    sed -i 's/echo "ASSERT \$s PASS ok"/echo "ASSERT $s FAIL wrong"/' "$T/guest.sh"
    run timeout 90 bash "$D09/run.sh" fake-vm
    [ "$status" -eq 1 ]
    [[ "$output" == *"RESULT FAIL"* ]]
}

@test "a waiting token left by an earlier dead driver is not served" {
    mkdir -p "$T/g"
    printf 's7-alarm.1.1' > "$T/g/waiting"
    # The fake guest publishes nothing until it claims; the real claim clears
    # `waiting`, so remove it the way the claim would.
    fake_guest 'VERDICT PASS' s1-quiet
    sed -i '2a rm -f "$QDLOCKER_09_DIR/waiting"; sleep 3' "$T/guest.sh"
    run timeout 90 bash "$D09/run.sh" fake-vm
    [ "$status" -eq 0 ]
    run cat "$T/served"
    [ "$output" = "capture s1 quiet" ]
}

@test "every host step guest.sh publishes has an action run.sh serves" {
    local names n bad=""
    names=$(grep -oE '^[[:space:]]*host [A-Za-z0-9_-]+|[;&|][[:space:]]*host [A-Za-z0-9_-]+|error\(\).*host [A-Za-z0-9_-]+' "$D09/guest.sh" \
        | grep -oE 'host [A-Za-z0-9_-]+$' | awk '{print $2}' | sort -u)
    [ -n "$names" ]
    for n in $names; do
        case "${n##*-}" in quiet|alarm|rec|drain|heads) ;; *) bad="$bad $n" ;; esac
    done
    [ -z "$bad" ] || { echo "unserved host steps:$bad"; false; }
    # The pixel-gated steps of the scenario keep their colour expectation.
    for n in s1-quiet s2-alarm s3-quiet s4-alarm s5-alarm s6-alarm s7-alarm s8b-alarm; do
        grep -qx "$n" <<<"$names" || { echo "missing host step $n"; false; }
    done
}

# gui-20260928T182835Z-1160277: pw-record ran as admin but was told to write
# into the root-created 0700 scratch dir, exited "Permission denied", and
# Step 2 read as a product FAIL. A redirection is opened by the ROOT driver
# shell and is fine; a path handed to an admin process as an argument must be
# in the admin-owned $ASCR.
admin_args_in_root_scratch() {
    grep -E '^[[:space:]]*(U|runuser)[[:space:]]' "$1" \
        | sed -E 's/[0-9]?>+[[:space:]]*"?\$SCR\/[^" ]*"?//g' \
        | grep -F '$SCR/' || true
}

@test "admin-run commands in guest.sh never get a root-only scratch path as an argument" {
    run admin_args_in_root_scratch "$D09/guest.sh"
    [ -z "$output" ] || { echo "admin writes into root scratch: $output"; false; }
    grep -q 'install -d -m 0700 -o admin -g users "\$ASCR"' "$D09/guest.sh"
}

@test "the recorders' liveness is checked before Steps 2/4/5 assert on the observer" {
    grep -q '^tracked_alive mic ' "$D09/guest.sh"
    grep -q '^    tracked_alive sysaudio ' "$D09/guest.sh"
    grep -q 'if ! tracked_alive cam; then' "$D09/guest.sh"
}

# ---------------------------------------------------------------------------
# The REAL guest.sh Setup, run against logging stubs and stopped at its first
# host step. Codex round 1 (2026-09-28): a qci_host_step timeout stops the
# driver WITHOUT its EXIT-trap teardown, so the next run's Setup must reclaim
# what that run left. sol round 2: ...but only what THIS scenario provably
# started or stopped: a foreign admin capture is reported, never killed (it
# would turn a real capture into Step 1's quiet baseline), and a session
# manager found stopped without this scenario's marker is reported, never
# silently repaired.
#
# The stubs map the guest's `admin` to the test user (pgrep), so "admin
# recorders" are real processes the test spawns: a copy of sleep named
# pw-record (so /proc/<pid>/comm is "pw-record").
run_guest_setup() {   # run_guest_setup <guest.sh>  (env: FAKE_SM, extra stubs)
    local g=$1 S="$T/stub"
    mkdir -p "$S" "$T/qd09"
    : > "$T/calls"
    for c in pkill busctl faillock install chown journalctl; do
        printf '#!/bin/bash\necho "%s $*" >>"%s/calls"\n' "$c" "$T" > "$S/$c"
    done
    # systemctl: log. The session manager is a small state machine: state
    # ($FAKE_SM, default active) and InvocationID ($FAKE_SM_INV, default
    # aaaa..), answered by `show -p ...` as key=value lines and by is-active.
    # start: new InvocationID (bbbb..); FAKE_SM_START_FAIL=1 makes it fail
    # into `failed` (still a new InvocationID). stop: inactive, same ID.
    # FAKE_SM_SHOW_FAIL=1 makes `show` fail.
    printf '%s\n' "${FAKE_SM:-active}" > "$T/sm-state"
    printf '%s\n' "${FAKE_SM_INV:-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}" > "$T/sm-inv"
    cat > "$S/systemctl" <<EOF
#!/bin/bash
echo "systemctl \$*" >>"$T/calls"
case "\$*" in
    show*qdistro-session-manager*)
        [ "\${FAKE_SM_SHOW_FAIL:-}" = 1 ] && exit 1
        printf 'LoadState=loaded\nActiveState=%s\nInvocationID=%s\n' "\$(cat "$T/sm-state")" "\$(cat "$T/sm-inv")" ;;
    *is-active*qdistro-session-manager*)
        st=\$(cat "$T/sm-state"); case "\$*" in *-q*) ;; *) echo "\$st" ;; esac
        [ "\$st" = active ] ;;
    *start*qdistro-session-manager*)
        echo bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb > "$T/sm-inv"
        if [ "\${FAKE_SM_START_FAIL:-}" = 1 ]; then echo failed > "$T/sm-state"; exit 1; fi
        echo active > "$T/sm-state" ;;
    *stop*qdistro-session-manager*)
        [ "\${FAKE_SM_STOP_FAIL:-}" = 1 ] && exit 1
        # FAKE_SM_STOP_NEWINV=1: another invocation ran and ended meanwhile
        [ "\${FAKE_SM_STOP_NEWINV:-}" = 1 ] && echo eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee > "$T/sm-inv"
        echo inactive > "$T/sm-state" ;;
    *is-active*) case "\$*" in *-q*) ;; *) echo active ;; esac ;;
esac
EOF
    # pgrep -u admin ... -> the real pgrep for the test user
    cat > "$S/pgrep" <<EOF
#!/bin/bash
a=(); while [ \$# -gt 0 ]; do if [ "\$1" = -u ] && [ "\$2" = admin ]; then a+=(-u "$(id -un)"); shift 2; else a+=("\$1"); shift; fi; done
exec /usr/bin/pgrep "\${a[@]}"
EOF
    # socat: the ctrl socket; `status` -> unlocked, `indicators` -> healthy
    printf '#!/bin/bash\nread -r cmd; case $cmd in status) echo "locked=False prompt-len=0";; *) echo "capture_observer=ok";; esac\n' > "$S/socat"
    # runuser -u admin -- CMD... | runuser -l admin -c "CMD"
    cat > "$S/runuser" <<'RU'
#!/bin/bash
if [ "$1" = -l ]; then exec bash -c "$4"; fi
while [ "$#" -gt 0 ] && [ "$1" != -- ]; do shift; done
shift; exec "$@"
RU
    chmod +x "$S"/*
    cat > "$T/waiters.sh" <<W
qci_claim_driver() { return 0; }
qci_host_step() { echo "HOSTSTEP \$1" >>"$T/calls"; trap - EXIT; exit 0; }
W
    PATH="$S:$PATH" QDLOCKER_09_TEST_HARNESS=1 QCI_GUI_WAITERS="$T/waiters.sh" \
        QDLOCKER_09_DIR="$T/qd09" timeout 60 bash "$g" >"$T/guest.out" 2>&1
}

# refute_grep <grep args...>: fail the test when the pattern IS found. A bare
# `! grep` does not fail a Bats test (SC2314): its status is ignored by -e.
refute_grep() {
    if grep "$@"; then echo "unexpected match: grep $*"; return 1; fi
    return 0
}

# first_line <pattern> -> line number of its first match in the call log
first_line() { grep -nF -- "$1" "$T/calls" | head -1 | cut -d: -f1; }

# spawn_recorder <comm>: a live process whose /proc/<pid>/comm is <comm>;
# prints "<pid> <starttime>"
spawn_recorder() {
    mkdir -p "$T/fakebin"
    cp /usr/bin/sleep "$T/fakebin/$1"
    "$T/fakebin/$1" 300 >/dev/null 2>&1 &
    local pid=$! st
    echo "$pid" >>"$T/spawned"
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        [ "$(cat "/proc/$pid/comm" 2>/dev/null)" = "$1" ] && break; sleep 0.1
    done
    read -r st <"/proc/$pid/stat"; set -f; set -- ${st##*") "}; set +f
    printf '%s %s\n' "$pid" "${20}"
}

teardown() {
    local p
    [ -f "$T/spawned" ] || return 0
    while read -r p; do kill -KILL "$p" 2>/dev/null || true; done <"$T/spawned"
}

@test "Setup stops THIS scenario's recorder (identity-checked) before the locker restart" {
    local pid st restart
    read -r pid st < <(spawn_recorder pw-record)
    mkdir -p "$T/qd09/scratch"
    printf '%s %s pw-record\n' "$pid" "$st" >"$T/qd09/scratch/rec-mic"
    run_guest_setup "$D09/guest.sh"
    grep -q '^HOSTSTEP setup-drain$' "$T/calls" || { cat "$T/guest.out"; false; }
    if kill -0 "$pid" 2>/dev/null; then echo "own recorder $pid still alive"; false; fi
    [ ! -e "$T/qd09/scratch/rec-mic" ]
    refute_grep -q 'foreign capture' "$T/guest.out"
}

@test "a foreign admin capture is REPORTED at Setup, never killed" {
    local pid st
    read -r pid st < <(spawn_recorder pw-record)
    # a stale record for a DIFFERENT process identity (wrong start time)
    mkdir -p "$T/qd09/scratch"
    printf '%s %s pw-record\n' "$pid" "$((st + 1))" >"$T/qd09/scratch/rec-mic"
    run_guest_setup "$D09/guest.sh"
    kill -0 "$pid" 2>/dev/null || { echo "foreign recorder $pid was killed"; false; }
    grep -q "foreign capture active before Step 1.*pw-record(pid $pid" "$T/guest.out" \
        || { cat "$T/guest.out"; false; }
    grep -q '^VERDICT ERROR' "$T/guest.out"
    refute_grep -q 'pkill' "$T/calls"
}

@test "a session manager stopped WITHOUT this scenario's marker is reported, not restarted" {
    FAKE_SM=inactive run_guest_setup "$D09/guest.sh"
    grep -q "qdistro-session-manager.service is 'inactive' at Setup and this scenario did not stop it" "$T/guest.out" \
        || { cat "$T/guest.out"; false; }
    refute_grep -q 'systemctl start qdistro-session-manager' "$T/calls"
    grep -q '^VERDICT ERROR' "$T/guest.out"
}

# mark <invocation-id>: this scenario's Step 8 marker, as sm_stop_verified writes it
mark() { mkdir -p "$T/qd09/scratch"; printf '%s\n' "$1" > "$T/qd09/scratch/sm-stopped-by-09"; }

@test "a session manager THIS scenario stopped (marker = its InvocationID) is restarted before the locker restart" {
    mark aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    FAKE_SM=inactive run_guest_setup "$D09/guest.sh"
    grep -q '^HOSTSTEP setup-drain$' "$T/calls" || { cat "$T/guest.out"; false; }
    local start restart
    start=$(first_line 'systemctl start qdistro-session-manager.service')
    restart=$(first_line 'systemctl --user restart qdlocker.service')
    [ -n "$start" ]
    [ -n "$restart" ]
    [ "$start" -lt "$restart" ]
    [ ! -e "$T/qd09/scratch/sm-stopped-by-09" ]
}

@test "a failed recovery keeps a verifiable marker (the failed attempt's InvocationID) and is reported as OUR stop" {
    mark aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    FAKE_SM=inactive FAKE_SM_START_FAIL=1 run_guest_setup "$D09/guest.sh"
    [ -e "$T/qd09/scratch/sm-stopped-by-09" ] || { echo "marker lost after a failed recovery"; cat "$T/guest.out"; false; }
    [ "$(cat "$T/qd09/scratch/sm-stopped-by-09")" = bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb ]
    grep -q "this scenario's own earlier stop could not be recovered" "$T/guest.out" || { cat "$T/guest.out"; false; }
    grep -q '^VERDICT ERROR' "$T/guest.out"
    # ...and the next attempt retries it (same invocation, still ours)
    : > "$T/calls"
    PATH="$T/stub:$PATH" QDLOCKER_09_TEST_HARNESS=1 QCI_GUI_WAITERS="$T/waiters.sh" \
        QDLOCKER_09_DIR="$T/qd09" timeout 60 bash "$D09/guest.sh" >"$T/guest.out" 2>&1 || true
    grep -q 'systemctl start qdistro-session-manager.service' "$T/calls"
}

@test "a marker whose InvocationID is not the manager's current one is not ours: no repair, reported" {
    # someone started (and something stopped) the manager after our stop
    mark aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    FAKE_SM=inactive FAKE_SM_INV=cccccccccccccccccccccccccccccccc run_guest_setup "$D09/guest.sh"
    refute_grep -q 'systemctl start qdistro-session-manager' "$T/calls"
    [ ! -e "$T/qd09/scratch/sm-stopped-by-09" ]
    grep -q "at Setup and this scenario did not stop it" "$T/guest.out" || { cat "$T/guest.out"; false; }
}

@test "a marker left after a COMPLETED recovery (manager active) is stale: dropped, nothing restarted" {
    mark aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    run_guest_setup "$D09/guest.sh"
    grep -q '^HOSTSTEP setup-drain$' "$T/calls" || { cat "$T/guest.out"; false; }
    refute_grep -q 'systemctl start qdistro-session-manager' "$T/calls"
    [ ! -e "$T/qd09/scratch/sm-stopped-by-09" ]
}

# load_sm: guest.sh's session-manager helpers, extracted, against the stub.
load_sm() {
    run_guest_setup "$D09/guest.sh"          # builds the stateful systemctl stub
    local f
    SCR="$T/sm"; mkdir -p "$SCR"; SM_MARK="$SCR/sm-stopped-by-09"; SM_UNIT=qdistro-session-manager.service
    for f in sm_read sm_stop_verified sm_try_start sm_reclaim; do
        eval "$(sed -n "/^$f() {/,/^}/p" "$D09/guest.sh")"
        declare -F "$f" >/dev/null || { echo "guest.sh has no $f()"; return 1; }
    done
    PATH="$T/stub:$PATH"
}

@test "Step 8 marks ONLY a verified stop: a failed stop, a failed query or a non-inactive state leave no marker" {
    load_sm
    echo active > "$T/sm-state"
    FAKE_SM_STOP_FAIL=1 sm_stop_verified && false
    [ ! -e "$SM_MARK" ]
    echo active > "$T/sm-state"
    FAKE_SM_SHOW_FAIL=1 sm_stop_verified && false
    [ ! -e "$SM_MARK" ]
    # a stop "succeeds" but the unit ends up failed, not inactive
    echo active > "$T/sm-state"
    printf '#!/bin/bash\necho "systemctl $*" >>"%s/calls"\ncase "$*" in show*) printf "LoadState=loaded\\nActiveState=failed\\nInvocationID=%s\\n" ;; esac\nexit 0\n' "$T" aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa > "$T/stub/systemctl.failed"
    chmod +x "$T/stub/systemctl.failed"
    systemctl() { "$T/stub/systemctl.failed" "$@"; }
    sm_stop_verified && false
    [ ! -e "$SM_MARK" ]
    unset -f systemctl
    # a clean, verified stop writes the invocation it stopped
    echo active > "$T/sm-state"
    sm_stop_verified
    [ "$(cat "$SM_MARK")" = aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ]
    # and the recovery clears it only once the manager reads active
    sm_try_start
    [ ! -e "$SM_MARK" ]
}

@test "QCI_GUI_WAITERS is ignored unless the test harness flag is set" {
    [ ! -e /tmp/qci-gui-waiters.sh ] || skip "a real /tmp/qci-gui-waiters.sh exists on this host"
    run_guest_setup "$D09/guest.sh"            # sanity: harness path works
    grep -q '^HOSTSTEP setup-drain$' "$T/calls"
    : > "$T/calls"
    PATH="$T/stub:$PATH" QCI_GUI_WAITERS="$T/waiters.sh" QDLOCKER_09_DIR="$T/qd09" \
        run timeout 30 bash "$D09/guest.sh"
    [ "$status" -eq 2 ]
    refute_grep -q HOSTSTEP "$T/calls"
    grep -q 'env -u QCI_GUI_WAITERS -u QDLOCKER_09_TEST_HARNESS' "$D09/run.sh"
}

# ---------------------------------------------------------------------------
# Probe helpers, extracted from guest.sh and run against stubs. A probe that
# FAILED must be distinguishable from one that succeeded with an empty
# result: only the latter may SKIP a conditional step.
load_probes() {
    local f
    SCR="$T/scr"; mkdir -p "$SCR" "$T/pstub"
    U() { "$@"; }
    for f in have compositor_journal probe_default_sink count_pw_nodes count_drm_outputs probe_node_id; do
        eval "$(sed -n "/^$f() {/,/^}/p" "$D09/guest.sh")"
        declare -F "$f" >/dev/null || { echo "guest.sh has no $f()"; return 1; }
    done
    cat > "$T/pstub/runuser" <<'RU'
#!/bin/bash
[ "$1" = -l ] && exec bash -c "$4"
exit 99
RU
    chmod +x "$T/pstub/runuser"
    PATH="$T/pstub:$PATH"
}
stub() { printf '#!/bin/bash\n%s\n' "$2" > "$T/pstub/$1"; chmod +x "$T/pstub/$1"; }

# Step 10 stubs. systemctl answers the compositor's InvocationID (or $2 if
# given, e.g. "" for none) and a start timestamp; journalctl answers the
# query scoped to THAT invocation with $1's lines, and ANY other query (a
# `--since` of the same second, the whole boot) with an earlier invocation's
# heads -- the one that crashed within the same second -- plus $1's.
CUR=dddddddddddddddddddddddddddddddd
drm_stubs() {
    local inv=${2-$CUR}
    printf '#!/bin/bash\ncase "$*" in *InvocationID*) echo "%s" ;; *ExecMainStartTimestamp*) echo @100 ;; esac\n' "$inv" > "$T/pstub/systemctl"
    printf '#!/bin/bash\ncase "$*" in *"_SYSTEMD_INVOCATION_ID=%s"*) printf "%%s\\n" %s ;; *) printf "%%s\\n" "Output Virtual-1 (crtc 39) video modes:" "Output Virtual-2 (crtc 40) video modes:" %s ;; esac\n' "$CUR" "$1" "$1" > "$T/pstub/journalctl"
    chmod +x "$T/pstub/systemctl" "$T/pstub/journalctl"
}

@test "Step 10 probe: counts DRM heads from the real log line; a failed or line-less query is a failure" {
    load_probes
    drm_stubs '"Output Virtual-1 (crtc 39) video modes:" "Output Virtual-2 (crtc 40) video modes:" "Output Virtual-1 (crtc 39) video modes:" "Output '"'"'pipewire-0'"'"' using color profile: x"'
    run count_drm_outputs; [ "$status" -eq 0 ]; [ "$output" = 2 ]
    stub journalctl 'echo "Failed to open journal" >&2; exit 1'
    run count_drm_outputs; [ "$status" -eq 2 ]
    drm_stubs '"output_created name=Virtual-1"'       # the old, never-logged pattern
    run count_drm_outputs; [ "$status" -eq 2 ]
    drm_stubs '"Output Virtual-1 (crtc 39) video modes:"' ""   # no InvocationID
    run count_drm_outputs; [ "$status" -eq 2 ]
    grep -q 'record 10 ERROR' "$D09/guest.sh"
}

@test "Step 10 probe: counts only THIS compositor invocation's heads, minus disabled ones" {
    load_probes
    # this invocation enabled Virtual-1 and Virtual-2, then disabled Virtual-2
    drm_stubs '"Output Virtual-1 (crtc 39) video modes:" "Output Virtual-2 (crtc 40) video modes:" "Disabling output Virtual-2"'
    run count_drm_outputs
    [ "$status" -eq 0 ]
    [ "$output" = 1 ] || { echo "counted $output enabled heads, want 1"; false; }
}

@test "Step 10 probe: an earlier invocation that crashed in the SAME second is not counted" {
    load_probes
    # the earlier invocation (same start second) enabled Virtual-1 and
    # Virtual-2 and crashed without disabling; this one enabled only Virtual-1
    drm_stubs '"Output Virtual-1 (crtc 39) video modes:"'
    run count_drm_outputs
    [ "$status" -eq 0 ]
    [ "$output" = 1 ] || { echo "counted $output heads, want 1 (a crashed same-second invocation leaked in)"; false; }
}

@test "Step 4 probe: pactl absent (3), failing (2), and succeeding-empty (0) are distinct" {
    load_probes
    local saved=$PATH
    PATH="$T/pstub:/nonexistent"
    run probe_default_sink; [ "$status" -eq 3 ]
    PATH=$saved
    stub pactl 'echo "Connection failure: Connection refused" >&2; exit 1'
    run probe_default_sink; [ "$status" -eq 2 ]
    stub pactl 'exit 0'
    run probe_default_sink; [ "$status" -eq 0 ]; [ -z "$output" ]
    stub pactl 'echo alsa_output.pci'
    run probe_default_sink; [ "$status" -eq 0 ]; [ "$output" = alsa_output.pci ]
    grep -q 'record 4 ERROR' "$D09/guest.sh"
}

@test "Steps 5/6 probes: a failing pw-cli is a failure, not zero nodes / no camera" {
    load_probes
    stub pw-cli 'exit 1'
    run count_pw_nodes; [ "$status" -eq 2 ]
    run probe_node_id cam0; [ "$status" -eq 2 ]
    stub pw-cli 'printf "%s\n" "id 45, type PipeWire:Interface:Node/3" "  node.name = \"cam0\"" "id 50, type x" "  node.name = \"weston.pipewire-1\""'
    run count_pw_nodes; [ "$status" -eq 0 ]; [ "$output" = 1 ]
    run probe_node_id cam0; [ "$status" -eq 0 ]; [ "$output" = 45 ]
    run probe_node_id nosuch; [ "$status" -eq 2 ]
    grep -q 'record 5 ERROR "camera probe failed' "$D09/guest.sh"
    grep -q 'record 6 ERROR' "$D09/guest.sh"
}

# ---------------------------------------------------------------------------
# run.sh's Step 10 decision, extracted and run against stubs: a failed or
# empty decode of the secondary frame is ERROR, never "uniformly black".
load_heads() {
    HLOG="$T/hlog"; CHECKS="$T/checks.tsv"; ART="$T/art"; VMNAME=fake
    : >"$CHECKS"
    check() { printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" >>"$CHECKS"; }
    mkdir -p "$T/hstub"
    printf '#!/bin/bash\n: > "$3"\n' > "$T/hstub/virsh"
    chmod +x "$T/hstub/virsh"
    QDWIN_VIRSH="$T/hstub/virsh"
    eval "$(sed -n '/^histogram() {/,/^}/p' "$D09/run.sh")"
    eval "$(sed -n '/^act_heads() {/,/^}/p' "$D09/run.sh")"
    PATH="$T/hstub:$PATH"
}

@test "Step 10: a failed or empty secondary decode is ERROR, never PASS" {
    load_heads
    printf '#!/bin/bash\necho "magick: improper image header" >&2\nexit 1\n' > "$T/hstub/magick"; chmod +x "$T/hstub/magick"
    act_heads s10
    grep -q $'^s10\tsecondary-black\tERROR' "$CHECKS" || { cat "$CHECKS"; false; }
    refute_grep -q $'secondary-black\tPASS' "$CHECKS"
    : >"$CHECKS"
    printf '#!/bin/bash\nexit 0\n' > "$T/hstub/magick"
    act_heads s10
    grep -q $'^s10\tsecondary-black\tERROR' "$CHECKS" || { cat "$CHECKS"; false; }
    : >"$CHECKS"
    printf '#!/bin/bash\necho "  100: (0,0,0) #000000 black"\n' > "$T/hstub/magick"
    act_heads s10
    grep -q $'^s10\tsecondary-black\tPASS' "$CHECKS"
    : >"$CHECKS"
    printf '#!/bin/bash\nprintf "%%s\\n" "  90: (0,0,0) #000000 black" "  10: (9,9,9) #090909 x"\n' > "$T/hstub/magick"
    act_heads s10
    grep -q $'^s10\tsecondary-black\tFAIL' "$CHECKS"
}

@test "an assertion FAIL followed by a driver timeout is RESULT ERROR, not FAIL" {
    fake_guest 'VERDICT PASS' s1-quiet
    # the driver fails 2.1, then dies before its verdict (as on a host-step timeout)
    sed -i 's/^for s in .*/qci_host_step s1-quiet 20; echo "ASSERT 2.1 FAIL capture_active=0"; exit 124/' "$T/guest.sh"
    run timeout 90 bash "$D09/run.sh" fake-vm
    [ "$status" -eq 3 ]
    [[ "$output" == *"RESULT ERROR"* ]]
    [[ "$output" == *"ASSERT 2.1 FAIL"* ]]
}

# ---------------------------------------------------------------------------
# sol round 3. run.sh's capture grading and the shared colour-count helper.

# load_capture: run.sh's act_capture, extracted, with the REAL colour-count
# helper from qdlocker-helpers.sh and a stubbed qdwin_screenshot.
load_capture() {
    HLOG="$T/hlog"; CHECKS="$T/checks.tsv"; ART="$T/art"; ERR_COLOR='#FD4663'
    : >"$CHECKS"
    check() { printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" >>"$CHECKS"; }
    eval "$(sed -n '/^qdlocker_count_color_in_crop() {/,/^}/p' "$REPO_ROOT/qdlocker/tests/gui/qdlocker-helpers.sh")"
    eval "$(sed -n '/^act_capture() {/,/^}/p' "$D09/run.sh")"
    qdlocker_screenshot_dimensions() { printf '1280 800'; }
    mkdir -p "$T/cstub"
    PATH="$T/cstub:$PATH"
}
# a frame whose banner band is solid #FD4663 (a real alarm), via real magick
alarm_png() { magick -size 1280x800 'xc:#FD4663' "PNG24:$1"; }

@test "a retained frame (.meta live=0) is ERROR for a post-action check, and its pixels are not graded" {
    command -v magick >/dev/null || skip "ImageMagick not on this host"
    load_capture
    # s7-alarm: the capture returns the compositor's RETAINED frame -- which
    # happens to show an alarm from an earlier step
    qdwin_screenshot() { alarm_png "$1"; echo "live=0 age_ms=9000 msc=7" > "$1.meta"; }
    act_capture s7 alarm
    grep -q $'^s7\tbanner-alarm\tERROR\tretained frame' "$CHECKS" || { cat "$CHECKS"; false; }
    refute_grep -q $'\tPASS\t' "$CHECKS"
    # a fresh frame with the same pixels is graded normally
    : >"$CHECKS"
    qdwin_screenshot() { rm -f "$1.meta"; alarm_png "$1"; }
    act_capture s7 alarm
    grep -q $'^s7\tbanner-alarm\tPASS' "$CHECKS" || { cat "$CHECKS"; false; }
}

@test "qdlocker_count_color_in_crop fails on a failed or empty decode instead of printing 0" {
    load_capture
    : > "$T/frame.png"
    printf '#!/bin/bash\necho "magick: improper image header" >&2\nexit 1\n' > "$T/cstub/magick"
    chmod +x "$T/cstub/magick"
    run qdlocker_count_color_in_crop "$T/frame.png" '#FD4663' 10x10+0+0
    [ "$status" -ne 0 ] || { echo "failed decode returned status 0, output '$output'"; false; }
    printf '#!/bin/bash\nexit 0\n' > "$T/cstub/magick"          # "succeeds", no histogram
    run qdlocker_count_color_in_crop "$T/frame.png" '#FD4663' 10x10+0+0
    [ "$status" -ne 0 ] || { echo "empty histogram returned status 0, output '$output'"; false; }
    printf '#!/bin/bash\nprintf "%%s\\n" "  60: (253,70,99) #FD4663 srgb(253,70,99)" "  40: (0,0,0) #000000 black"\n' > "$T/cstub/magick"
    run qdlocker_count_color_in_crop "$T/frame.png" '#FD4663' 10x10+0+0
    [ "$status" -eq 0 ]; [ "$output" = 60 ]
    # ...so banner-quiet cannot PASS on pixels nobody counted
    printf '#!/bin/bash\nexit 1\n' > "$T/cstub/magick"
    qdwin_screenshot() { : > "$1"; echo x > "$1"; rm -f "$1.meta"; }
    : >"$CHECKS"
    act_capture s3 quiet
    grep -q $'^s3\tbanner\tERROR' "$CHECKS" || { cat "$CHECKS"; false; }
}

@test "a declared guest FAIL with no ASSERT FAIL row is still RESULT FAIL" {
    fake_guest 'VERDICT FAIL (rows lost)' s1-quiet
    run timeout 90 bash "$D09/run.sh" fake-vm
    [ "$status" -eq 1 ] || { echo "$output"; false; }
    [[ "$output" == *"RESULT FAIL"* ]]
}

# ---------------------------------------------------------------------------
# The REAL guest.sh from Setup through Step 5, with pactl and gst-launch-1.0
# hidden (QDLOCKER_09_HIDE_TOOLS, honoured only under the harness flag) and a
# camera node present: a missing TOOL is not evidence of no sink / no camera,
# so both conditional steps must be ERROR, never SKIP.
@test "a missing pactl, or a missing gst-launch-1.0 with a camera present, is ERROR, not SKIP" {
    local S="$T/stub"
    run_guest_setup "$D09/guest.sh"          # builds the stubs; stops at setup-drain
    # recorders: scripts, so /proc/<pid>/comm is their name; TERM ends them
    for r in pw-record parec; do
        printf '#!/bin/bash\ntrap "kill \\$! 2>/dev/null; exit 0" TERM\nsleep 300 & wait\n' > "$S/$r"
    done
    # python3: the isolated-mode import answers an installed path; the camera
    # probe (reading its program from stdin) reports a camera node
    printf '#!/bin/bash\ncase "$*" in *" -c "*) echo /usr/lib64/python3.13/site-packages/qdlocker/indicators.py ;; *) cat >/dev/null; echo cam0 ;; esac\n' > "$S/python3"
    printf '#!/bin/bash\nexit 0\n' > "$S/pw-cli"
    # install -d creates directories only inside the test tree
    printf '#!/bin/bash\necho "install $*" >>"%s/calls"\nd=${@: -1}\ncase "$*" in *-d*) case $d in %s/*) mkdir -p "$d" ;; esac ;; esac\nexit 0\n' "$T" "$T" > "$S/install"
    chmod +x "$S"/*
    cat > "$T/waiters.sh" <<W
qci_claim_driver() { return 0; }
qci_host_step() { echo "HOSTSTEP \$1" >>"$T/calls"; [ "\$1" = s7-alarm ] || [ "\$1" = cleanup-drain ] || return 0; trap - EXIT; exit 0; }
W
    : > "$T/calls"
    PATH="$S:$PATH" QDLOCKER_09_TEST_HARNESS=1 QCI_GUI_WAITERS="$T/waiters.sh" \
        QDLOCKER_09_HIDE_TOOLS="pactl gst-launch-1.0" QDLOCKER_09_DIR="$T/qd09" \
        timeout 120 bash "$D09/guest.sh" >"$T/guest.out" 2>&1 || true
    pkill -f -- "$S/pw-record" 2>/dev/null || true
    grep -q '^ASSERT 4 ERROR pactl is not installed' "$T/guest.out" || { cat "$T/guest.out"; false; }
    grep -q '^ASSERT 5 ERROR camera node cam0 is present but gst-launch-1.0 is not installed' "$T/guest.out" \
        || { cat "$T/guest.out"; false; }
    refute_grep -qE '^ASSERT [45] SKIP' "$T/guest.out"
}

# ---------------------------------------------------------------------------
# sol round 5.
@test "Step 8 does not claim a manager that was ALREADY inactive: no stop, no marker" {
    load_sm
    echo inactive > "$T/sm-state"          # it exited on its own after 8.2
    : > "$T/calls"
    if sm_stop_verified; then echo "claimed a stop it did not perform"; false; fi
    [ ! -e "$SM_MARK" ]
    refute_grep -q 'systemctl stop qdistro-session-manager' "$T/calls"
}

@test "Step 8 does not claim a stop when the post-stop InvocationID differs from the pre-stop one" {
    load_sm
    echo active > "$T/sm-state"
    if FAKE_SM_STOP_NEWINV=1 sm_stop_verified; then echo "claimed a different invocation"; false; fi
    [ ! -e "$SM_MARK" ]
}

@test "Step 8 skips the recovery start unless THIS scenario verifiably stopped the manager" {
    local gate stopped start
    stopped=$(grep -n '^if sm_stop_verified; then$' "$D09/guest.sh" | cut -d: -f1)
    gate=$(grep -n '^if \[ "\$SM_STOPPED" != 1 \]; then$' "$D09/guest.sh" | cut -d: -f1)
    start=$(grep -n '^elif ! sm_try_start; then$' "$D09/guest.sh" | cut -d: -f1)
    [ -n "$stopped" ]; [ -n "$gate" ]; [ -n "$start" ]
    [ "$stopped" -lt "$gate" ]
    [ "$gate" -lt "$start" ]
}

@test "an unreadable manager state KEEPS this scenario's marker (sm_reclaim) and Setup reports ERROR" {
    load_sm
    printf '%s\n' aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa > "$SM_MARK"
    FAKE_SM_SHOW_FAIL=1 sm_reclaim
    [ -e "$SM_MARK" ] || { echo "marker discarded on a failed query"; false; }
    # the whole Setup
    mark aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    FAKE_SM_SHOW_FAIL=1 run_guest_setup "$D09/guest.sh"
    [ -e "$T/qd09/scratch/sm-stopped-by-09" ] || { echo "marker discarded by Setup"; cat "$T/guest.out"; false; }
    grep -q "cannot query qdistro-session-manager.service state at Setup; this scenario's marker is kept" "$T/guest.out" \
        || { cat "$T/guest.out"; false; }
    grep -q '^VERDICT ERROR' "$T/guest.out"
    refute_grep -q 'systemctl start qdistro-session-manager' "$T/calls"
}

@test "7.3 searches only journal entries after a cursor taken BEFORE the injected hang" {
    local cur inject q
    cur=$(grep -n '^C7=\$(runuser -l admin -c "journalctl --user -u qdlocker.service -n1 --show-cursor' "$D09/guest.sh" | cut -d: -f1)
    inject=$(grep -n '^install -d -m 0755 "\$BROKEN"' "$D09/guest.sh" | cut -d: -f1)
    q=$(grep -n -- "--after-cursor='\$C7'" "$D09/guest.sh" | cut -d: -f1)
    [ -n "$cur" ]; [ -n "$inject" ]; [ -n "$q" ]
    [ "$cur" -lt "$inject" ]
    [ "$inject" -lt "$q" ]
    # no whole-boot search for the timeout line remains
    refute_grep -q 'qdlocker.service --boot' "$D09/guest.sh"
    # the cursor check accepts a real cursor and rejects junk
    local re
    re=$(sed -n 's/^if ! \[\[ \$C7 =~ \(.*\) \]\]; then$/\1/p' "$D09/guest.sh")
    [ -n "$re" ]
    [[ 's=5b1e0c6a1d3a4f0e9c7b2a1d0e9f8c7b;i=1a2f3;b=0f1e2d3c4b5a69788796a5b4c3d2e1f0;m=4a5b6c7;t=62a1b2c3d4e5f;x=9f8e7d6c5b4a3920' =~ $re ]]
    if [[ "'; rm -rf /" =~ $re ]]; then false; fi
}

@test "guest.sh and run.sh parse" {
    bash -n "$D09/guest.sh"
    bash -n "$D09/run.sh"
}
