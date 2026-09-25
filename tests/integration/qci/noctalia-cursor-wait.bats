#!/usr/bin/env bats
#
# Host-only poll test for scenario 04's cursor-remap waiter
# (tests/integration/qdwin-noctalia/noctalia-helpers.sh). No VM: the journal
# is a plain file. The waiter must re-read that file until a post-cursor
# `mapped on cursor_layer` line with nonzero_alpha>0 appears, and fail loud
# when the deadline expires. A single longer sleep is not the contract.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/tests/integration/qdwin-noctalia/noctalia-helpers.sh"
    NOCT_CURSOR_POLL_S=0.05
    NOCT_CURSOR_JOURNAL_FILE="$BATS_TEST_TMPDIR/journal.txt"
}

_remap_line() {
    printf 'qdwin: %s: mapped on cursor_layer (hotspot=1,1) payload=32x32 nonzero_alpha=%s\n' "$1" "$2"
}

@test "default cursor wait bound is 10s" {
    env -u NOCT_CURSOR_WAIT_S -u NOCT_CURSOR_POLL_S -u NOCT_CURSOR_JOURNAL_FILE \
        bash -c '
            source "$1"
            [ "$NOCT_CURSOR_WAIT_S" = 10 ]
            [ "$NOCT_CURSOR_POLL_S" = 0.25 ]
        ' bash "$REPO_ROOT/tests/integration/qdwin-noctalia/noctalia-helpers.sh"
}

@test "count predicate matches nonzero_alpha tokens, not alpha 0 or unrelated lines" {
    {
        echo "noise nonzero_alpha=9"
        _remap_line install_default_cursor 0
        _remap_line "cursor-shape install shape=default" 10
        echo "qdwin: install_default_cursor: mapped on cursor_layer (hotspot=0,0) payload=none"
        echo "qdwin: x: mapped on cursor_layer (hotspot=0,0) payload=8x8 nonzero_alpha=-1"
        _remap_line install_default_cursor 2
    } > "$NOCT_CURSOR_JOURNAL_FILE"
    run noct_count_cursor_layer_nonzero_alpha < "$NOCT_CURSOR_JOURNAL_FILE"
    [ "$status" -eq 0 ]
    [ "$output" = "2" ]
}

@test "cursor_layer_nonzero_alpha_after ignores remaps before the cursor" {
    {
        _remap_line install_default_cursor 16
        echo "-- cursor: cur-step"
        _remap_line install_default_cursor 0
    } > "$NOCT_CURSOR_JOURNAL_FILE"
    run cursor_layer_nonzero_alpha_after cur-step
    [ "$status" -eq 0 ]
    [ "$output" = "0" ]
    _remap_line install_default_cursor 4 >> "$NOCT_CURSOR_JOURNAL_FILE"
    run cursor_layer_nonzero_alpha_after cur-step
    [ "$status" -eq 0 ]
    [ "$output" = "1" ]
}

@test "waiter returns as soon as a post-cursor nonzero remap is already present" {
    {
        _remap_line install_default_cursor 16
        echo "-- cursor: cur-step"
        _remap_line "cursor-shape install shape=left_ptr" 10
    } > "$NOCT_CURSOR_JOURNAL_FILE"
    local start end
    start=$(date +%s%3N)
    run noct_wait_cursor_layer_nonzero_alpha cur-step 5
    end=$(date +%s%3N)
    [ "$status" -eq 0 ]
    [[ "$output" != *FAIL* ]]
    # Must not burn the deadline; the line is already in the file.
    [ $(( end - start )) -lt 1000 ]
}

@test "waiter polls until a remap line appears, then returns before the deadline" {
    echo "-- cursor: cur-step" > "$NOCT_CURSOR_JOURNAL_FILE"
    (
        sleep 0.35
        _remap_line install_default_cursor 8 >> "$NOCT_CURSOR_JOURNAL_FILE"
    ) &
    local start end
    start=$(date +%s%3N)
    run noct_wait_cursor_layer_nonzero_alpha cur-step 3
    end=$(date +%s%3N)
    wait
    [ "$status" -eq 0 ]
    [[ "$output" != *FAIL* ]]
    # A sleep-the-whole-bound waiter would still be inside its 3s sleep.
    [ $(( end - start )) -lt 1500 ]
}

@test "waiter fails loud when the deadline expires with no nonzero remap" {
    {
        _remap_line install_default_cursor 16
        echo "-- cursor: cur-step"
        _remap_line install_default_cursor 0
        echo "qdwin: x: mapped on cursor_layer (hotspot=0,0) payload=none"
    } > "$NOCT_CURSOR_JOURNAL_FILE"
    local start end
    start=$(date +%s%3N)
    run noct_wait_cursor_layer_nonzero_alpha cur-step 0.4
    end=$(date +%s%3N)
    [ "$status" -eq 1 ]
    [[ "$output" == *"FAIL:"* ]]
    [[ "$output" == *"mapped on cursor_layer"* ]]
    [[ "$output" == *"nonzero_alpha>0"* ]]
    [[ "$output" == *"last_count=0"* ]]
    [[ "$output" == *"cursor=cur-step"* ]]
    [ $(( end - start )) -ge 400 ]
    [ $(( end - start )) -lt 2000 ]
}

# Production path: NOCT_CURSOR_JOURNAL_FILE unset. A stub vm-exec records the
# remote command and returns only the lines after the cursor that command
# actually names. Dropping --after-cursor, the unit, or this cursor makes the
# stub return the pre-move remap, so the count is 1 instead of 0.
@test "vm journal path passes --after-cursor to qdwin-compositor and drops earlier remaps" {
    unset NOCT_CURSOR_JOURNAL_FILE
    local full="$BATS_TEST_TMPDIR/full.journal"
    local cmdfile="$BATS_TEST_TMPDIR/last-cmd"
    local exe="$BATS_TEST_TMPDIR/vm-exec"
    {
        _remap_line install_default_cursor 16
        echo "-- cursor: cur-step"
        _remap_line install_default_cursor 0
    } > "$full"
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
if not m:
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
    run cursor_layer_nonzero_alpha_after cur-step
    [ "$status" -eq 0 ]
    [ "$output" = "0" ]
    [ -f "$cmdfile" ]
    grep -F "runuser -l admin -c" "$cmdfile"
    grep -F "journalctl --user -u qdwin-compositor.service --after-cursor 'cur-step' --no-pager" "$cmdfile"
    _remap_line install_default_cursor 4 >> "$full"
    run cursor_layer_nonzero_alpha_after cur-step
    [ "$status" -eq 0 ]
    [ "$output" = "1" ]
}

@test "waiter does not treat a missing journal file as success" {
    NOCT_CURSOR_JOURNAL_FILE="$BATS_TEST_TMPDIR/missing-journal.txt"
    run noct_wait_cursor_layer_nonzero_alpha cur-step 0.3
    [ "$status" -eq 1 ]
    [[ "$output" == *"FAIL:"* ]]
    [[ "$output" == *"last_count=0"* ]]
}
