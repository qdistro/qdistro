#!/usr/bin/env bats
#
# Host-only tests of qdwin/tests/gui/22-nested-proxy-teardown.d/run.sh, the
# committed HOST runner of qdwin gui/22 -- the REAL functions, sourced from the
# file (QD22_RUNSH_LIB=1 returns before main), never a copy of them.
#
# What they pin:
#  - the S3 launcher does not pin vm-exec until the probe exits (qga reports a
#    command finished only once every holder of its stdout/stderr has closed
#    them; the launcher used to keep them, and every preview was taken after
#    the click timeout had destroyed the proxy -- 2026-09-17..09-24);
#  - a probe that exits before waiting (rc=77 "no pointer on the seat") is
#    never mistaken for a waiting one: no CLICK_TARGET is accepted after rc=,
#    and the preview guard refuses once rc= is in the log
#    (gui-20260930T114916Z-4162368: a hand-translated guest driver skipped the
#    host-only pointer priming and graded a frame of a proxy destroyed 1 ms
#    after it was created);
#  - the S3 pixel gate passes the real proxy colours and fails a black frame;
#  - the runner primes the pointer before S1, and the probe checks for a
#    pointer BEFORE it prints CLICK_TARGET.
#
# The fake vm-exec reads the guest command's output through a command
# substitution, which waits for EOF on the pipe exactly as qga does.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    RUNSH="$REPO_ROOT/qdwin/tests/gui/22-nested-proxy-teardown.d/run.sh"
    SCENARIO="$REPO_ROOT/qdwin/tests/gui/22-nested-proxy-teardown.md"
    PROBE_C="$REPO_ROOT/qdwin/test-client/qdwin-nested-probe.c"
    T="$BATS_TEST_TMPDIR"
    mkdir -p "$T/bin"
    cat > "$T/bin/fake-vm-exec" <<'EOF'
#!/bin/bash
out=$(bash -c "$2" 2>&1); rc=$?
printf '%s' "$out"
exit "$rc"
EOF
    # runuser -u admin -- CMD...  ->  CMD...
    cat > "$T/bin/runuser" <<'EOF'
#!/bin/bash
while [ "$#" -gt 0 ] && [ "$1" != -- ]; do shift; done
shift
exec "$@"
EOF
    # A probe shaped like the real one: PROXY_GEOM, then either the 77 exit
    # (no pointer -- and, like the fixed probe, NO click target) or
    # CLICK_TARGET and a wait.
    cat > "$T/bin/qdwin-nested-probe" <<'EOF'
#!/bin/bash
echo "PROXY_GEOM x=240 y=100 w=800 h=600 output=Virtual-1 out=1280x800@0,0 outputs=3 scale=1 transform=0 side=N chrome=32"
if [ "${FAKE_NO_POINTER:-0}" = 1 ]; then
    echo "qdwin-nested-probe: no pointer on the seat" >&2
    exit 77
fi
echo "CLICK_TARGET x=640 y=84"
echo "CLICK_TARGET_GLOBAL x=640 y=84"
sleep "${FAKE_PROBE_S:-20}"
exit 77
EOF
    chmod +x "$T/bin/"*
    export PATH="$T/bin:$PATH"
    export QDWIN_VM_EXEC="$T/bin/fake-vm-exec" VMNAME=fake-vm
    export ACTIVE_SOCKET=wayland-1 QD22_OUTPUT=Virtual-1
    export QD22_LOG="$T/qd22.log" QD22_LAUNCH_LOG="$T/qd22.launch.log" QD22_PID="$T/qd22.pid"
    export QD22_CANCEL="$T/qd22.cancel" QD22_INTENT="$T/qd22.intent" QD22_LAUNCHER="$T/qd22.launch.sh"
    QD22_RUNSH_LIB=1 source "$RUNSH"
}

teardown() {
    local p
    p=$(cat "$QD22_PID" 2>/dev/null) || true
    [ -n "$p" ] && kill -TERM -- "-$p" 2>/dev/null || true
    true
}

@test "sourcing run.sh as a library defines the S3 parts and does nothing else" {
    declare -F qd22_s3_stage qd22_s3_launch qd22_s3_ack qd22_s3_target qd22_s3_waiting qd22_s3_pixels >/dev/null
    [ ! -e "$QD22_LOG" ]
}

@test "the fake vm-exec is pinned by a background job that keeps its stdio (the measure is real)" {
    local start=$SECONDS
    "$QDWIN_VM_EXEC" "$VMNAME" "sleep 3 &" >/dev/null
    [ $((SECONDS - start)) -ge 2 ]
}

@test "S3 launch returns while the probe is still waiting (launcher does not pin vm-exec)" {
    local start
    export FAKE_PROBE_S=20
    qd22_s3_stage
    start=$SECONDS
    qd22_s3_launch
    [ $((SECONDS - start)) -lt 5 ]
    qd22_s3_ack
    [ -n "$PROBE_PID" ]
    qd22_s3_target
    [ "$TARGET" = "CLICK_TARGET x=640 y=84" ]
    run qd22_s3_waiting
    [ "$status" -eq 0 ]
}

@test "the launcher writes the probe's NUMERIC status, not a literal \$?" {
    export FAKE_PROBE_S=0
    qd22_s3_stage
    qd22_s3_launch
    for _ in $(seq 1 40); do grep -q '^rc=' "$QD22_LOG" 2>/dev/null && break; sleep 0.1; done
    grep -qx 'rc=77' "$QD22_LOG"
    [ "$(qd22_rc_of "$(cat "$QD22_LOG")")" = 77 ]
}

@test "S3 launch honours the cancel flag and starts no probe" {
    qd22_s3_stage
    touch "$QD22_CANCEL"
    qd22_s3_launch
    sleep 0.5
    [ ! -e "$QD22_PID" ]
    [ ! -e "$QD22_LOG" ]
}

@test "no pointer: the probe exits without CLICK_TARGET and the target wait stops at rc=, not after 20s" {
    local start
    export FAKE_NO_POINTER=1
    qd22_s3_stage
    qd22_s3_launch
    qd22_s3_ack
    start=$SECONDS
    run qd22_s3_target
    [ "$status" -eq 1 ]
    [ $((SECONDS - start)) -lt 10 ]
    [[ "$output" == *"never printed CLICK_TARGET"* ]]
    [[ "$output" == *"no pointer on the seat"* ]]
}

@test "preview guard: an exited probe (proxy gone) refuses the preview" {
    export FAKE_PROBE_S=0
    qd22_s3_stage
    qd22_s3_launch
    qd22_s3_ack
    for _ in $(seq 1 40); do grep -q '^rc=' "$QD22_LOG" 2>/dev/null && break; sleep 0.1; done
    run qd22_s3_waiting
    [ "$status" -eq 1 ]
    [[ "$output" == *"already exited"* ]]
    [[ "$output" == *"rc=77"* ]]
}

@test "a launcher failure before the probe starts is reported, and the launch still returns promptly" {
    cat > "$T/bin/runuser" <<'SHIM'
#!/bin/bash
echo "RUNUSER-FAILED: injected" >&2
exit 1
SHIM
    chmod +x "$T/bin/runuser"
    local start
    qd22_s3_stage
    start=$SECONDS
    qd22_s3_launch
    [ $((SECONDS - start)) -lt 5 ]
    run qd22_s3_ack
    [ "$status" -eq 1 ]
    [[ "$output" == *"never published its pid"* ]]
    [[ "$output" == *"RUNUSER-FAILED: injected"* ]]
}

@test "waiting guard: a failed or empty guest read is NOT 'still waiting'" {
    export FAKE_PROBE_S=20
    qd22_s3_stage
    qd22_s3_launch
    qd22_s3_ack
    qd22_s3_target
    run qd22_s3_waiting
    [ "$status" -eq 0 ]
    # the transport fails: no output, nonzero status
    cat > "$T/bin/broken-vm-exec" <<'EOF'
#!/bin/bash
exit 1
EOF
    chmod +x "$T/bin/broken-vm-exec"
    QDWIN_VM_EXEC="$T/bin/broken-vm-exec" run qd22_s3_waiting
    [ "$status" -eq 1 ]
    [[ "$output" == *"could not read"* ]]
    # the transport fails AFTER printing WAITING: still not evidence
    cat > "$T/bin/lying-vm-exec" <<'EOF'
#!/bin/bash
echo WAITING
exit 1
EOF
    chmod +x "$T/bin/lying-vm-exec"
    QDWIN_VM_EXEC="$T/bin/lying-vm-exec" run qd22_s3_waiting
    [ "$status" -eq 1 ]
    [[ "$output" == *"vm-exec rc=1"* ]]
    # an unreadable log (grep status 2) is not "no rc="
    chmod 000 "$QD22_LOG"
    if ! grep -q x "$QD22_LOG" 2>/dev/null && [ "$(id -u)" -ne 0 ]; then
        run qd22_s3_waiting
        chmod 644 "$QD22_LOG"
        [ "$status" -eq 1 ]
        [[ "$output" == *"could not be read"* ]]
    fi
    chmod 644 "$QD22_LOG"
}

@test "waiting guard: a missing log, or a dead group with no rc=, is not 'still waiting'" {
    export FAKE_PROBE_S=20
    qd22_s3_stage
    qd22_s3_launch
    qd22_s3_ack
    qd22_s3_target
    kill -KILL -- "-$PROBE_PID"
    sleep 0.3
    run qd22_s3_waiting
    [ "$status" -eq 1 ]
    [[ "$output" == *"is gone"* ]]
    rm -f "$QD22_LOG"
    run qd22_s3_waiting
    [ "$status" -eq 1 ]
    [[ "$output" == *"does not exist"* ]]
    PROBE_PID= run qd22_s3_waiting
    [ "$status" -eq 1 ]
}

@test "an orphaned probe from an earlier run is reaped via the guest run record" {
    export FAKE_PROBE_S=30
    qd22_s3_stage
    qd22_s3_launch
    qd22_s3_ack
    local orphan=$PROBE_PID
    kill -0 -- "-$orphan"
    # the record the earlier runner left, with its paths in the default layout
    local prev=11-22-33
    QD22_CURRENT="$T/current"
    echo "$prev" > "$QD22_CURRENT"
    mv "$QD22_PID" "/tmp/qd22-popup.$prev.pid"
    touch "/tmp/qd22-popup.$prev.intent"
    run qd22_reap_previous
    rm -f /tmp/qd22-popup.$prev.*
    [ "$status" -eq 0 ]
    [[ "$output" == *"reaped"* || "$output" == *"killed"* ]]
    ! kill -0 -- "-$orphan" 2>/dev/null
}

@test "run.sh refuses a second copy on the same VM (host lock) before touching anything" {
    local art="$T/art" lock
    mkdir -p "$art"
    # The runner still locks by VM name; keep this fake VM's lock private to
    # the test so another Unix user running the suite cannot own its inode.
    export TMPDIR="$T"
    lock="$TMPDIR/qd22-runner.fake-vm.lock"
    exec {fd}>>"$lock"
    flock -n "$fd"
    QCI_GUI_ARTIFACT_DIR="$art" run bash "$RUNSH" fake-vm
    exec {fd}>&-
    [ "$status" -eq 3 ]
    [[ "$output" == *"another run.sh is already driving"* ]]
    [ ! -e "$art/asserts.tsv" ]
}

@test "run.sh refuses a reused artifact dir and leaves the earlier frames untouched" {
    local art="$T/art2"
    mkdir -p "$art"
    printf 'earlier' > "$art/s3-preview.png"
    QCI_GUI_ARTIFACT_DIR="$art" run bash "$RUNSH" fake-vm
    [ "$status" -eq 3 ]
    [[ "$output" == *"refusing to overwrite evidence"* ]]
    [ "$(cat "$art/s3-preview.png")" = earlier ]
}

# ------------------------------------------------------------- pixel gate
GEOM="PROXY_GEOM x=240 y=100 w=800 h=600 output=Virtual-1 out=1280x800@0,0 outputs=3 scale=1 transform=0 side=N chrome=32"

@test "pixel gate: the proxy as the golden draws it (teal band over #333847) passes" {
    command -v magick >/dev/null || skip "no ImageMagick"
    magick -size 1280x800 xc:black -fill '#333847' -draw 'rectangle 240,100 1039,699' \
        -fill '#00aaaa' -draw 'rectangle 240,68 1039,99' "$T/ok.png"
    run qd22_s3_pixels "$T/ok.png" "$GEOM" 640 84
    [ "$status" -eq 0 ]
    [[ "$output" == *"~ #00aaaa"* ]]
}

@test "pixel gate: an all-black frame (the 2026-09-30 preview) fails" {
    command -v magick >/dev/null || skip "no ImageMagick"
    magick -size 1280x800 xc:black "$T/black.png"
    run qd22_s3_pixels "$T/black.png" "$GEOM" 640 84
    [ "$status" -eq 1 ]
    [[ "$output" == *"proxy NOT in frame"* ]]
}

@test "pixel gate: the proxy body without its chrome band fails (the band is what gets clicked)" {
    command -v magick >/dev/null || skip "no ImageMagick"
    magick -size 1280x800 xc:black -fill '#333847' -draw 'rectangle 240,100 1039,699' "$T/noband.png"
    run qd22_s3_pixels "$T/noband.png" "$GEOM" 640 84
    [ "$status" -eq 1 ]
}

@test "pixel gate: the body point honours the head's origin" {
    command -v magick >/dev/null || skip "no ImageMagick"
    # Head at global (1280,0): the proxy at global x=1520 is local x=240.
    magick -size 1280x800 xc:black -fill '#333847' -draw 'rectangle 240,100 1039,699' \
        -fill '#00aaaa' -draw 'rectangle 240,68 1039,99' "$T/ok.png"
    run qd22_s3_pixels "$T/ok.png" \
        "PROXY_GEOM x=1520 y=100 w=800 h=600 output=Virtual-1 out=1280x800@1280,0 outputs=3 scale=1 transform=0 side=N chrome=32" 640 84
    [ "$status" -eq 0 ]
}

@test "pixel gate: an undecodable frame or an unparsable PROXY_GEOM is undecidable (2), never a verdict" {
    printf 'not a png' > "$T/bad.png"
    run qd22_s3_pixels "$T/bad.png" "$GEOM" 640 84
    [ "$status" -eq 2 ]
    run qd22_s3_pixels "$T/bad.png" "PROXY_GEOM garbage" 640 84
    [ "$status" -eq 2 ]
}

@test "S4 typing sends each key's press AND release in one QMP call, and reports a failed injection" {
    cat > "$T/bin/fake-virsh" <<'EOF'
#!/bin/bash
printf '%s\n' "$3" >> "$FAKE_VIRSH_LOG"
ok='{"return":{},"id":"libvirt-1"}'
printf '%s\n' "${FAKE_VIRSH_REPLY:-$ok}"
exit "${FAKE_VIRSH_RC:-0}"
EOF
    chmod +x "$T/bin/fake-virsh"
    export FAKE_VIRSH_LOG="$T/virsh.log"
    QDWIN_VIRSH="$T/bin/fake-virsh"
    run qd22_type ab
    [ "$status" -eq 0 ]
    [ "$(wc -l < "$FAKE_VIRSH_LOG")" -eq 2 ]
    local k line
    for k in a b; do
        line=$(grep -F "\"data\":\"$k\"" "$FAKE_VIRSH_LOG")
        [[ "$line" == *'"down":true,"key":{"type":"qcode","data":"'$k'"}'* ]]
        [[ "$line" == *'"down":false,"key":{"type":"qcode","data":"'$k'"}'* ]]
    done
    FAKE_VIRSH_RC=1 run qd22_type a
    [ "$status" -eq 1 ]
    [[ "$output" == *"injection of 'a' failed"* ]]
    # virsh succeeds but QEMU rejected the event: still a failed injection
    FAKE_VIRSH_REPLY='{"id":"libvirt-2","error":{"class":"GenericError","desc":"nope"}}' run qd22_type a
    [ "$status" -eq 1 ]
    [[ "$output" == *"GenericError"* ]]
    run qd22_type 'a!'
    [ "$status" -eq 2 ]
}

# ------------------------------------------------------------ structure
@test "run.sh primes the pointer after taking the shell role and before S1's probe" {
    local take prime s1
    take=$(grep -n '^qdwin_apps_prepare_shell_probe' "$RUNSH" | head -1 | cut -d: -f1)
    prime=$(grep -n '^qdwin_prime_pointer' "$RUNSH" | head -1 | cut -d: -f1)
    s1=$(grep -n 'qd22_probe --destroy-with-move' "$RUNSH" | head -1 | cut -d: -f1)
    [ -n "$take" ] && [ -n "$prime" ] && [ -n "$s1" ]
    [ "$take" -lt "$prime" ]
    [ "$prime" -lt "$s1" ]
}

@test "run.sh takes the S3 frame only between two 'still waiting' checks, and grades it before clicking" {
    local w1 shot w2 pix click
    w1=$(grep -n 'msg=$(qd22_s3_waiting)' "$RUNSH" | sed -n 1p | cut -d: -f1)
    shot=$(grep -n 'qdwin_apps_screenshot "$ART/s3-preview.png"' "$RUNSH" | cut -d: -f1)
    w2=$(grep -n 'msg=$(qd22_s3_waiting)' "$RUNSH" | sed -n 2p | cut -d: -f1)
    pix=$(grep -n 'pix=$(qd22_s3_pixels' "$RUNSH" | cut -d: -f1)
    click=$(grep -n '^qdwin_click "$CX" "$CY" left' "$RUNSH" | cut -d: -f1)
    [ -n "$w1" ] && [ -n "$shot" ] && [ -n "$w2" ] && [ -n "$pix" ] && [ -n "$click" ]
    [ "$w1" -lt "$shot" ] && [ "$shot" -lt "$w2" ] && [ "$w2" -lt "$pix" ] && [ "$pix" -lt "$click" ]
}

@test "the popup probe checks for a pointer BEFORE it prints CLICK_TARGET" {
    local chk tgt
    chk=$(grep -n 'show_popup needs a live pointer grab' "$PROBE_C" | head -1 | cut -d: -f1)
    tgt=$(grep -n 'printf("CLICK_TARGET x=' "$PROBE_C" | head -1 | cut -d: -f1)
    [ -n "$chk" ] && [ -n "$tgt" ]
    [ "$chk" -lt "$tgt" ]
}

@test "the scenario sends the runner to run.sh and carries no second copy of the S3 launch" {
    grep -q '22-nested-proxy-teardown.d/run.sh' "$SCENARIO"
    ! grep -q -- '--destroy-with-popup --click-timeout' "$SCENARIO"
}
