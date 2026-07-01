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

# --- new rules (PR5) ---

@test "flake-lint: fires bare-relative-source (unquoted and quoted)" {
    local f="$BATS_TEST_TMPDIR/33-src.md"
    printf '# x\n```bash\nsource tests/lib/helpers.sh\ngrep -q ok /x || exit 1\n```\n' > "$f"
    run lint "$f"; [[ "$output" == *"bare-relative-source"* ]]
    # Quoted relative paths have the same cwd-sensitivity and must fire too.
    printf '# x\n```bash\nsource "./helpers.sh"\n. '\''../lib/x.sh'\''\ngrep -q ok /x || exit 1\n```\n' > "$f"
    run lint "$f"; [[ "$output" == *"bare-relative-source"* ]]
}

@test "flake-lint: absolute / \$-anchored source is NOT bare-relative-source" {
    local f="$BATS_TEST_TMPDIR/34-src-ok.md"
    printf '# x\n```bash\nsource /tmp/qci-gui-waiters.sh\nsource "$ROOT/helpers.sh"\ngrep -q ok /x || exit 1\n```\n' > "$f"
    run lint "$f"
    ! [[ "$output" == *"bare-relative-source"* ]]
}

@test "flake-lint: fires pgrep-self-match across flag forms, not with a bracket guard" {
    local bad="$BATS_TEST_TMPDIR/35-pgrep.md" good="$BATS_TEST_TMPDIR/36-pgrep.md"
    # -f anywhere in the flags, extra options, and a quoted multi-word pattern.
    printf '# x\n```bash\npgrep -u admin -f qdistro_admin_broker || exit 1\n$V "pgrep -f '\''qs -p /run/x'\'' | head" || exit 1\n```\n' > "$bad"
    # A bracket guard suppresses it; -x (exact, not -f) is not a self-match risk.
    printf '# x\n```bash\npgrep -f "[q]distro_admin_broker" || exit 1\npgrep -x qdshell || exit 1\n```\n' > "$good"
    run lint "$bad"; [[ "$output" == *"pgrep-self-match"* ]]
    run lint "$good"; ! [[ "$output" == *"pgrep-self-match"* ]]
}

@test "flake-lint: fires unscoped-tmp-path on a fixed /tmp write, not a scoped one" {
    local bad="$BATS_TEST_TMPDIR/37-tmp.md" good="$BATS_TEST_TMPDIR/38-tmp.md"
    printf '# x\n```bash\npgrep -x app > /tmp/07-pid.txt\ngrep -q 1 /tmp/07-pid.txt || exit 1\n```\n' > "$bad"
    printf '# x\n```bash\npgrep -x app > "$QCI_SCENARIO_TMPDIR/pid.txt"\ngrep -q 1 "$QCI_SCENARIO_TMPDIR/pid.txt" || exit 1\n```\n' > "$good"
    run lint "$bad"; [[ "$output" == *"unscoped-tmp-path"* ]]
    run lint "$good"; ! [[ "$output" == *"unscoped-tmp-path"* ]]
}

@test "flake-lint: fires screenshot-only-assert when no structured probe exists" {
    local shot="$BATS_TEST_TMPDIR/39-shot.md" mixed="$BATS_TEST_TMPDIR/40-shot.md"
    printf '# x\n```bash\ngrim /out.png\ntesseract /out.png - | grep -q Ready || exit 1\n```\n' > "$shot"
    printf '# x\n```bash\ngrim /out.png\njournalctl --after-cursor "$c" | grep -q mapped || exit 1\n```\n' > "$mixed"
    run lint "$shot"; [[ "$output" == *"screenshot-only-assert"* ]]
    run lint "$mixed"; ! [[ "$output" == *"screenshot-only-assert"* ]]
}

@test "flake-lint: fires backgrounded-wait, incl. the '& pid=\$!' capture form" {
    local f="$BATS_TEST_TMPDIR/41-bg.md"
    printf '# x\n```bash\nawait_qs_ready & pid=$!\ngrep -q ok /x || exit 1\n```\n' > "$f"
    run lint "$f"
    [[ "$output" == *"backgrounded-wait"* ]]
}

@test "flake-lint: 'sleep N; cmd &' does NOT flag the sleep as backgrounded" {
    # The sleep is foreground; only cmd is backgrounded. Must not false-positive.
    local f="$BATS_TEST_TMPDIR/41b-bg.md"
    printf '# x\n```bash\nsleep 1; notify_ready &\ngrep -q ok /x || exit 1\n```\n' > "$f"
    run lint "$f"
    ! [[ "$output" == *"backgrounded-wait"* ]]
}

@test "flake-lint: the original clean waiter scenario stays clean under new rules" {
    run lint "$CLEAN"
    [ "$status" -eq 0 ]
    ! [[ "$output" == *"$CLEAN:"* ]]
}

# --- allowlist ---

@test "flake-lint: an allowlisted finding does not fail --strict" {
    local f="$BATS_TEST_TMPDIR/42-tmp.md" al="$BATS_TEST_TMPDIR/allow.tsv"
    printf '# x\n```bash\necho x > /tmp/fixed.txt\ngrep -q 1 /tmp/fixed.txt || exit 1\n```\n' > "$f"
    # Without an allowlist, --strict fails.
    run lint --strict --no-allowlist "$f"
    [ "$status" -ne 0 ]
    # Waive just that rule for that file -> --strict passes, finding still shown.
    printf '42-tmp.md\tunscoped-tmp-path\ttest fixture\n' > "$al"
    run lint --strict --allowlist "$al" "$f"
    [ "$status" -eq 0 ]
    [[ "$output" == *"[allowed: test fixture]"* ]]
}

@test "flake-lint: a per-rule waiver does NOT suppress a co-located real bug" {
    # An explicit visual test waives screenshot-only-assert only; a real
    # unscoped-tmp-path in the same file must still fail --strict.
    local f="$BATS_TEST_TMPDIR/43-vis.md" al="$BATS_TEST_TMPDIR/allow2.tsv"
    printf '# x\n```bash\ngrim /out.png\necho x > /tmp/fixed.txt\ntesseract /out.png - | grep -q Ready || exit 1\n```\n' > "$f"
    printf '43-vis.md\tscreenshot-only-assert\texplicit visual-rendering oracle\n' > "$al"
    run lint --strict --allowlist "$al" "$f"
    [ "$status" -ne 0 ]                                   # unscoped-tmp-path still fails
    [[ "$output" == *"unscoped-tmp-path"* ]]
    [[ "$output" == *"[allowed: explicit visual-rendering oracle]"* ]]
}

@test "flake-lint: allowlist rule=* waives every rule for a path" {
    local f="$BATS_TEST_TMPDIR/44-exempt.md" al="$BATS_TEST_TMPDIR/allow3.tsv"
    printf '# x\n```bash\ngrim /out.png\necho x > /tmp/fixed.txt\ntesseract /out.png - | grep -q Ready || exit 1\n```\n' > "$f"
    printf '44-exempt.md\t*\twholly outside the lint contract\n' > "$al"
    run lint --strict --allowlist "$al" "$f"
    [ "$status" -eq 0 ]
}
