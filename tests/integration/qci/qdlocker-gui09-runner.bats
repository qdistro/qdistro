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

@test "guest.sh and run.sh parse" {
    bash -n "$D09/guest.sh"
    bash -n "$D09/run.sh"
}
