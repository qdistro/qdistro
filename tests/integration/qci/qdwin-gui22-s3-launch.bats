#!/usr/bin/env bats
#
# Host-only test of qdwin/tests/gui/22-nested-proxy-teardown.md's S3 launch
# statement -- the REAL text from the scenario, extracted and executed, not a
# copy of it.
#
# The defect it pins: the S3 launcher backgrounded `setsid sh -c '...' &` inside
# a vm-exec command without detaching the launcher's own stdio. vm-exec runs
# through qga guest-exec with capture-output, which reports a command finished
# only once every holder of its stdout/stderr pipes has closed them, so vm-exec
# did not return until the PROBE exited -- after its click timeout, with the
# proxy destroyed. The driver then took the S3 preview of an empty screen: the
# "pure-black S3" ERROR in every gui/22 run from 2026-09-17 to 09-24
# (todo/test-blankscreenshots/qdwin-gui22-triage.md, live probe 2026-09-25).
#
# The fake vm-exec below reads the guest command's output through a command
# substitution, which waits for EOF on the pipe exactly as qga does (the same
# measure gui-waiters.bats uses for bg_start).

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    SCENARIO="$REPO_ROOT/qdwin/tests/gui/22-nested-proxy-teardown.md"
    T="$BATS_TEST_TMPDIR"
    mkdir -p "$T/bin"
    # qga-like vm-exec: run the guest command, return only at EOF of its output.
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
    # A probe that prints its click target and then waits, like the real one.
    cat > "$T/bin/qdwin-nested-probe" <<'EOF'
#!/bin/bash
echo "CLICK_TARGET x=640 y=84"
sleep "${FAKE_PROBE_S:-20}"
exit 77
EOF
    chmod +x "$T/bin/"*
    export PATH="$T/bin:$PATH"
    export QDWIN_VM_EXEC="$T/bin/fake-vm-exec" VMNAME=fake-vm
    export ACTIVE_SOCKET=wayland-1 QD22_OUTPUT=Virtual-1
    export QD22_LOG="$T/qd22.log" QD22_PID="$T/qd22.pid"
    export QD22_CANCEL="$T/qd22.cancel" QD22_INTENT="$T/qd22.intent"
}

teardown() {
    local p
    p=$(cat "$QD22_PID" 2>/dev/null) || true
    [ -n "$p" ] && kill -TERM -- "-$p" 2>/dev/null || true
    true
}

# The one statement in the scenario that launches the popup probe: from its
# `"$QDWIN_VM_EXEC" "$VMNAME" \` line through the host-side `>/dev/null`.
extract_launch() {
    awk '
        /^"\$QDWIN_VM_EXEC" "\$VMNAME" \\$/ { buf = $0 "\n"; grab = 1; next }
        grab { buf = buf $0 "\n"
               if ($0 ~ /^  >\/dev\/null$/) { if (buf ~ /--destroy-with-popup/) { printf "%s", buf; n++ } grab = 0 } }
        END { if (n != 1) exit 1 }
    ' "$SCENARIO"
}

@test "the S3 launch statement is found exactly once in the scenario" {
    run extract_launch
    [ "$status" -eq 0 ]
    [[ "$output" == *"setsid sh -c"* ]]
    [[ "$output" == *"--destroy-with-popup"* ]]
}

@test "the fake vm-exec is pinned by a background job that keeps its stdio (the measure is real)" {
    # Sanity for the harness itself: a backgrounded job holding the pipe must
    # hold this fake vm-exec, or the test below could never fail.
    local start=$SECONDS
    "$QDWIN_VM_EXEC" "$VMNAME" "sleep 3 &" >/dev/null
    [ $((SECONDS - start)) -ge 2 ]
}

@test "S3 launch returns while the probe is still waiting (launcher does not pin vm-exec)" {
    local stmt start elapsed
    stmt=$(extract_launch)
    export FAKE_PROBE_S=20
    start=$SECONDS
    eval "$stmt"
    elapsed=$((SECONDS - start))
    echo "launch took ${elapsed}s" >&3 2>/dev/null || true
    [ "$elapsed" -lt 5 ]
    # The probe was started and is still waiting: pid published, CLICK_TARGET
    # printed, no rc= yet -- i.e. the proxy would still be on screen.
    for _ in $(seq 1 40); do [ -s "$QD22_PID" ] && grep -q '^CLICK_TARGET ' "$QD22_LOG" 2>/dev/null && break; sleep 0.1; done
    [ -s "$QD22_PID" ]
    grep -q '^CLICK_TARGET ' "$QD22_LOG"
    ! grep -q '^rc=' "$QD22_LOG"
}

@test "S3 launch honours the cancel flag and starts no probe" {
    local stmt
    stmt=$(extract_launch)
    touch "$QD22_CANCEL"
    eval "$stmt"
    sleep 0.5
    [ ! -e "$QD22_PID" ]
    [ ! -e "$QD22_LOG" ]
}

# The guard between CLICK_TARGET and any preview: refuse to preview once the
# probe has exited (the rc=77 "no pointer" path prints CLICK_TARGET first).
extract_exited_guard() {
    awk '
        /^if "\$QDWIN_VM_EXEC" "\$VMNAME" "grep -q .\^rc=. \$QD22_LOG"; then$/ { buf = $0 "\n"; grab = 1; next }
        grab { buf = buf $0 "\n"; if ($0 ~ /^fi$/) { printf "%s", buf; n++; grab = 0 } }
        END { if (n != 1) exit 1 }
    ' "$SCENARIO"
}

@test "preview guard: an exited probe (proxy gone) is an ERROR, not a preview" {
    local stmt guard
    stmt=$(extract_launch)
    guard=$(extract_exited_guard)
    export FAKE_PROBE_S=0
    eval "$stmt"
    for _ in $(seq 1 40); do grep -q '^rc=' "$QD22_LOG" 2>/dev/null && break; sleep 0.1; done
    run bash -c "$guard"
    [ "$status" -eq 1 ]
    [[ "$output" == *"already exited"* ]]
    [[ "$output" == *"rc=77"* ]]
}

@test "preview guard: a probe still waiting passes through" {
    local stmt guard
    stmt=$(extract_launch)
    guard=$(extract_exited_guard)
    export FAKE_PROBE_S=20
    eval "$stmt"
    for _ in $(seq 1 40); do grep -q '^CLICK_TARGET ' "$QD22_LOG" 2>/dev/null && break; sleep 0.1; done
    run bash -c "$guard"
    [ "$status" -eq 0 ]
}
