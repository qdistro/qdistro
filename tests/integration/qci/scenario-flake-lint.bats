#!/usr/bin/env bats
#
# Host-only tests for ci/bin/scenario-flake-lint.py — the warn-only flake-smell
# linter. Lints fixture markdown scenarios with known smells and asserts each
# rule fires (and that a clean, waiter-using scenario produces none). Confirms
# warn-only: a dirty scenario exits 0 unless --strict.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    SCRIPT="$REPO_ROOT/ci/bin/scenario-flake-lint.py"
    DIRTY="$BATS_TEST_TMPDIR/30-dirty.md"
    CLEAN="$BATS_TEST_TMPDIR/31-clean.md"
    cat > "$DIRTY" <<'MD'
# Dirty scenario

## Steps

```bash
sleep 5
grep ready /tmp/out

systemctl --user is-active qdshell.service

virsh domstate mydom

VM=$(virsh list --name | head -1)

journalctl --user --since "2 min ago" | grep mapped
```
MD
    cat > "$CLEAN" <<'MD'
# Clean scenario

## Steps

```bash
source /tmp/qci-gui-waiters.sh
await_user_unit_active qdshell.service
await_file /tmp/out 10
grep -q ready /tmp/out || exit 1
```
MD
}

lint() { python3 "$SCRIPT" "$@"; }

@test "flake-lint: fires sleep-before-assert" {
    run lint "$DIRTY"
    [[ "$output" == *"sleep-before-assert"* ]]
}

@test "flake-lint: fires oneshot-systemctl" {
    run lint "$DIRTY"
    [[ "$output" == *"oneshot-systemctl"* ]]
}

@test "flake-lint: fires oneshot-domstate" {
    run lint "$DIRTY"
    [[ "$output" == *"oneshot-domstate"* ]]
}

@test "flake-lint: fires virsh-head-vm-select" {
    run lint "$DIRTY"
    [[ "$output" == *"virsh-head-vm-select"* ]]
}

@test "flake-lint: fires unscoped-journal" {
    run lint "$DIRTY"
    [[ "$output" == *"unscoped-journal"* ]]
}

@test "flake-lint: a waiter-using scenario is clean" {
    run lint "$CLEAN"
    [ "$status" -eq 0 ]
    # No gcc-format finding lines for the clean file.
    ! [[ "$output" == *"$CLEAN:"* ]]
}

@test "flake-lint: warn-only exits 0 even on a dirty scenario" {
    run lint "$DIRTY"
    [ "$status" -eq 0 ]
}

@test "flake-lint: --strict exits nonzero on findings" {
    run lint --strict "$DIRTY"
    [ "$status" -ne 0 ]
}

@test "flake-lint: prose-only scenario (code blocks, no shell assertion) is flagged" {
    local prose="$BATS_TEST_TMPDIR/32-prose.md"
    cat > "$prose" <<'MD'
# Prose only

## Steps

```bash
echo "launch the app and look at the screen"
ls /tmp
```
MD
    run lint "$prose"
    [[ "$output" == *"prose-only-assert"* ]]
}
