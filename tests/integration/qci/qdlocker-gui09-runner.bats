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
    # systemctl: log; is-active answers "active", or $FAKE_SM for the manager
    cat > "$S/systemctl" <<EOF
#!/bin/bash
echo "systemctl \$*" >>"$T/calls"
case "\$*" in
    *is-active*qdistro-session-manager*) echo "\${FAKE_SM:-active}" ;;
    *is-active*) echo active ;;
esac
exit 0
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
    ! kill -0 "$pid" 2>/dev/null || { echo "own recorder $pid still alive"; false; }
    [ ! -e "$T/qd09/scratch/rec-mic" ]
    ! grep -q 'foreign capture' "$T/guest.out"
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
    ! grep -q 'pkill' "$T/calls"
}

@test "a session manager stopped WITHOUT this scenario's marker is reported, not restarted" {
    FAKE_SM=inactive run_guest_setup "$D09/guest.sh"
    grep -q "qdistro-session-manager.service is 'inactive' at Setup and this scenario did not stop it" "$T/guest.out" \
        || { cat "$T/guest.out"; false; }
    ! grep -q 'systemctl start qdistro-session-manager' "$T/calls"
    grep -q '^VERDICT ERROR' "$T/guest.out"
}

@test "a session manager THIS scenario stopped (marker) is restarted before the locker restart" {
    mkdir -p "$T/qd09/scratch"
    : >"$T/qd09/scratch/sm-stopped-by-09"
    run_guest_setup "$D09/guest.sh"
    grep -q '^HOSTSTEP setup-drain$' "$T/calls" || { cat "$T/guest.out"; false; }
    local start restart
    start=$(first_line 'systemctl start qdistro-session-manager.service')
    restart=$(first_line 'systemctl --user restart qdlocker.service')
    [ -n "$start" ] && [ -n "$restart" ] && [ "$start" -lt "$restart" ]
    [ ! -e "$T/qd09/scratch/sm-stopped-by-09" ]
}

@test "QCI_GUI_WAITERS is ignored unless the test harness flag is set" {
    [ ! -e /tmp/qci-gui-waiters.sh ] || skip "a real /tmp/qci-gui-waiters.sh exists on this host"
    run_guest_setup "$D09/guest.sh"            # sanity: harness path works
    grep -q '^HOSTSTEP setup-drain$' "$T/calls"
    : > "$T/calls"
    PATH="$T/stub:$PATH" QCI_GUI_WAITERS="$T/waiters.sh" QDLOCKER_09_DIR="$T/qd09" \
        run timeout 30 bash "$D09/guest.sh"
    [ "$status" -eq 2 ]
    ! grep -q HOSTSTEP "$T/calls"
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
    for f in compositor_journal probe_default_sink count_pw_nodes count_drm_outputs probe_node_id; do
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

@test "Step 10 probe: counts DRM heads from the real log line; a failed or line-less query is a failure" {
    load_probes
    stub journalctl 'printf "%s\n" "Output Virtual-1 (crtc 39) video modes:" "Output Virtual-2 (crtc 40) video modes:" "Output Virtual-1 (crtc 39) video modes:" "Output '"'"'pipewire-0'"'"' using color profile: x"'
    run count_drm_outputs; [ "$status" -eq 0 ]; [ "$output" = 2 ]
    stub journalctl 'echo "Failed to open journal" >&2; exit 1'
    run count_drm_outputs; [ "$status" -eq 2 ]
    stub journalctl 'echo "output_created name=Virtual-1"'     # the old, never-logged pattern
    run count_drm_outputs; [ "$status" -eq 2 ]
    grep -q 'record 10 ERROR' "$D09/guest.sh"
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
    ! grep -q $'secondary-black\tPASS' "$CHECKS"
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

@test "guest.sh and run.sh parse" {
    bash -n "$D09/guest.sh"
    bash -n "$D09/run.sh"
}
