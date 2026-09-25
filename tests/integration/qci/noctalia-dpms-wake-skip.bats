#!/usr/bin/env bats
#
# Host-only matcher for scenario 05's virtio-gpu DPMS-on skip
# (tests/integration/qdwin-noctalia/noctalia-helpers.sh). No VM: the journal
# is a plain file. Success means the weston_log EINVAL line is present and
# the scenario may print SKIP and exit 0. Anything else is not a skip.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/tests/integration/qdwin-noctalia/noctalia-helpers.sh"
    NOCT_DPMS_WAKE_POLL_S=0.05
    NOCT_CURSOR_JOURNAL_FILE="$BATS_TEST_TMPDIR/journal.txt"
    SCENARIO="$REPO_ROOT/tests/integration/qdwin-noctalia/05-bar-stays-after-idle.md"
}

_einval_line() {
    printf '%satomic: couldn'\''t commit new state: Invalid argument\n' "$1"
}

@test "default DPMS wake poll bound is 5s" {
    env -u NOCT_DPMS_WAKE_WAIT_S -u NOCT_DPMS_WAKE_POLL_S -u NOCT_CURSOR_JOURNAL_FILE \
        bash -c '
            source "$1"
            [ "$NOCT_DPMS_WAKE_WAIT_S" = 5 ]
            [ "$NOCT_DPMS_WAKE_POLL_S" = 0.25 ]
        ' bash "$REPO_ROOT/tests/integration/qdwin-noctalia/noctalia-helpers.sh"
}

@test "journalctl-prefixed EINVAL commit record is a skip" {
    {
        echo "Sep 25 12:34:55 vmhost qdwin-compositor[4312]: atomic: couldn't compile atomic state"
        _einval_line "Sep 25 12:34:56 vmhost qdwin-compositor[4312]: "
    } > "$NOCT_CURSOR_JOURNAL_FILE"
    run noct_dpms_on_atomic_einval < "$NOCT_CURSOR_JOURNAL_FILE"
    [ "$status" -eq 0 ]
    [ -z "$output" ]

    _einval_line "" > "$NOCT_CURSOR_JOURNAL_FILE"
    run noct_dpms_on_atomic_einval < "$NOCT_CURSOR_JOURNAL_FILE"
    [ "$status" -eq 0 ]
    [ -z "$output" ]

    # Trailing whitespace and a CR still end on the strerror text.
    printf 'Sep 25 12:34:56 vmhost qdwin[1]: atomic: couldn'\''t commit new state: Invalid argument \r\n' \
        > "$NOCT_CURSOR_JOURNAL_FILE"
    run noct_dpms_on_atomic_einval < "$NOCT_CURSOR_JOURNAL_FILE"
    [ "$status" -eq 0 ]
}

@test "couldn't compile atomic state is not a skip" {
    echo "Sep 25 12:34:56 vmhost qdwin-compositor[4312]: atomic: couldn't compile atomic state" \
        > "$NOCT_CURSOR_JOURNAL_FILE"
    run noct_dpms_on_atomic_einval < "$NOCT_CURSOR_JOURNAL_FILE"
    [ "$status" -eq 1 ]
    [ -z "$output" ]
}

@test "commit failure with a different errno is not a skip" {
    {
        echo "Sep 25 12:34:56 vmhost qdwin-compositor[4312]: atomic: couldn't commit new state: Device or resource busy"
        echo "Sep 25 12:34:57 vmhost qdwin-compositor[4312]: atomic: couldn't commit new state: Permission denied"
    } > "$NOCT_CURSOR_JOURNAL_FILE"
    run noct_dpms_on_atomic_einval < "$NOCT_CURSOR_JOURNAL_FILE"
    [ "$status" -eq 1 ]
    [ -z "$output" ]
}

@test "EINVAL reason inside quotes or not at end of line is not a skip" {
    cat > "$NOCT_CURSOR_JOURNAL_FILE" <<'EOF'
peer_label="atomic: couldn't commit new state: Invalid argument"
diagnostic: "atomic: couldn't commit new state: Invalid argument"
note "atomic: couldn't commit new state: Invalid argument"
atomic: couldn't commit new state: Invalid argument (ignored)
notatomic: couldn't commit new state: Invalid argument
EOF
    run noct_dpms_on_atomic_einval < "$NOCT_CURSOR_JOURNAL_FILE"
    [ "$status" -eq 1 ]
    [ -z "$output" ]
}

@test "empty journal is not a skip" {
    : > "$NOCT_CURSOR_JOURNAL_FILE"
    run noct_dpms_on_atomic_einval < "$NOCT_CURSOR_JOURNAL_FILE"
    [ "$status" -eq 1 ]
    [ -z "$output" ]

    echo "-- cursor: wake-cur" > "$NOCT_CURSOR_JOURNAL_FILE"
    run noct_dpms_on_atomic_einval_after wake-cur
    [ "$status" -eq 1 ]
    [ -z "$output" ]
}

@test "EINVAL before the wake cursor is not a skip" {
    cat > "$NOCT_CURSOR_JOURNAL_FILE" <<'EOF'
Sep 25 12:00:00 vmhost qdwin-compositor[1]: atomic: couldn't commit new state: Invalid argument
-- cursor: wake-cur
Sep 25 12:01:00 vmhost qdwin-compositor[1]: atomic: couldn't compile atomic state
EOF
    run noct_dpms_on_atomic_einval_after wake-cur
    [ "$status" -eq 1 ]
    [ -z "$output" ]

    local start end
    start=$(date +%s%3N)
    run noct_poll_dpms_on_atomic_einval wake-cur 0.4
    end=$(date +%s%3N)
    [ "$status" -eq 1 ]
    [ -z "$output" ]
    [ $(( end - start )) -ge 400 ]
    [ $(( end - start )) -lt 2000 ]
}

@test "poll returns as soon as the post-cursor EINVAL record is present" {
    cat > "$NOCT_CURSOR_JOURNAL_FILE" <<'EOF'
Sep 25 12:00:00 vmhost qdwin-compositor[1]: atomic: couldn't commit new state: Invalid argument
-- cursor: wake-cur
Sep 25 12:01:00 vmhost qdwin-compositor[1]: atomic: couldn't compile atomic state
Sep 25 12:01:01 vmhost qdwin-compositor[1]: atomic: couldn't commit new state: Invalid argument
EOF
    local start end
    start=$(date +%s%3N)
    run noct_poll_dpms_on_atomic_einval wake-cur 5
    end=$(date +%s%3N)
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    [ $(( end - start )) -lt 1000 ]
}

@test "poll returns when the EINVAL record appears during the window" {
    echo "-- cursor: wake-cur" > "$NOCT_CURSOR_JOURNAL_FILE"
    (
        sleep 0.35
        printf '%s\n' "Sep 25 12:02:00 vmhost qdwin-compositor[1]: atomic: couldn't commit new state: Invalid argument" \
            >> "$NOCT_CURSOR_JOURNAL_FILE"
    ) &
    local start end
    start=$(date +%s%3N)
    run noct_poll_dpms_on_atomic_einval wake-cur 3
    end=$(date +%s%3N)
    wait
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    [ $(( end - start )) -lt 1500 ]
}

@test "scenario and helper document that the skip shell exits 0" {
    grep -F 'shell exit on this SKIP is 0' \
        "$REPO_ROOT/tests/integration/qdwin-noctalia/noctalia-helpers.sh"
    grep -F 'Do not exit 77' \
        "$REPO_ROOT/tests/integration/qdwin-noctalia/noctalia-helpers.sh"
    grep -F 'shell exit on this SKIP is 0' "$SCENARIO"
    awk '
        /echo "SKIP: virtio-gpu rejected the DPMS-on atomic commit/ { hit = NR; next }
        hit && NR == hit + 1 {
            if ($0 ~ /exit 0/ && $0 !~ /exit 77/) ok = 1
        }
        END { exit ok ? 0 : 1 }
    ' "$SCENARIO"
    ! grep -E '(^|[^[:digit:]])exit[[:space:]]+77([^[:digit:]]|$)' "$SCENARIO"
    # Capable-host bar and error-line asserts stay in the scenario.
    grep -F '**Assert (3.1):** bar visible in top 31 px again.' "$SCENARIO"
    grep -F '**Assert (4.1):** zero `error <N>:` lines in the captured log.' "$SCENARIO"
    grep -F 'reads exactly `Off`' "$SCENARIO"
}

# Production path: NOCT_CURSOR_JOURNAL_FILE unset. The only EINVAL line is
# before the cursor. A command that drops --after-cursor or the unit filter
# fails the stub's extractor and the matcher then sees that earlier line.
@test "wake poll journalctl is unit-scoped and cursor-scoped, with no grep" {
    unset NOCT_CURSOR_JOURNAL_FILE
    local full="$BATS_TEST_TMPDIR/full.journal"
    local cmdfile="$BATS_TEST_TMPDIR/last-cmd"
    local exe="$BATS_TEST_TMPDIR/vm-exec"
    cat > "$full" <<'EOF'
Sep 25 12:00:00 vmhost qdwin-compositor[1]: atomic: couldn't commit new state: Invalid argument
-- cursor: cur-step
Sep 25 12:01:00 vmhost qdwin-compositor[1]: atomic: couldn't compile atomic state
EOF
    cat > "$exe" <<EOF
#!/bin/bash
printf '%s\\n' "\$2" > "$cmdfile"
python3 - "\$2" "$full" <<'PY'
import re, sys
cmd, path = sys.argv[1], sys.argv[2]
text = open(path).read().splitlines(True)
m = re.search(
    r"journalctl --user -u qdwin-compositor\\.service --after-cursor '([^']*)' --no-pager",
    cmd,
)
if not m or "| grep" in cmd or " grep " in cmd:
    sys.stdout.write("".join(text))
    raise SystemExit(0)
cur = m.group(1)
out, seen = [], False
for line in text:
    if line == f"-- cursor: {cur}\\n":
        seen = True
        continue
    if seen:
        out.append(line)
sys.stdout.write("".join(out))
PY
EOF
    chmod +x "$exe"
    VMNAME=test-vm
    QDWIN_VM_EXEC="$exe"
    run noct_poll_dpms_on_atomic_einval cur-step 0.35
    [ "$status" -eq 1 ]
    [ -z "$output" ]
    [ -f "$cmdfile" ]
    grep -F "runuser -l admin -c" "$cmdfile"
    grep -F "journalctl --user -u qdwin-compositor.service --after-cursor 'cur-step' --no-pager" "$cmdfile"
    ! grep -F 'grep' "$cmdfile"
}
