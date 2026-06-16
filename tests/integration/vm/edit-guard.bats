#!/usr/bin/env bats
# qci edit-guard — CI-integrity protected-path guard.
#
# An agent tasked with fixing PRODUCT code must not silently edit the tests
# that grade it, or the prompts/policy that constrain it. `qci edit-guard`
# maps a changed-path set to the protected globs (tests/**, ci/prompts/**,
# selinux/**) and FAILS unless the edit is explicitly sanctioned as test/CI
# maintenance (--allow-test-edits or QCI_ALLOW_TEST_EDITS=1).
#
# Like qsu-binary.bats, this file does NOT `load helpers` (which hard-requires
# a live VM via VM_NAME). Every assertion runs on the dev host: it invokes the
# real `ci/bin/qci edit-guard` against explicit path sets and a throwaway git
# repo, so `bats tests/integration/vm/edit-guard.bats` passes without a VM.

REPO_ROOT="$(git -C "$(dirname "${BATS_TEST_FILENAME}")" rev-parse --show-toplevel 2>/dev/null)"
QCI="$REPO_ROOT/ci/bin/qci"

# Copy the real ci/ tree into a throwaway repo at $1. Since commit 8e4e0b1
# split the runner into a justfile + sourced modules, ci/bin/qci SOURCES
# ci/lib/*.sh at startup; a throwaway repo with only ci/bin/qci makes that
# sourcing fail and leaves main() undefined (exit 127, "main: command not
# found"). Copying the whole ci/ tree makes ci/lib/ present so the dispatcher
# behaves identically to the real repo. We exclude the gitignored ci/runs/
# artifacts dir (potentially large local run logs that qci never reads — tests
# override QCI_RUNS_DIR anyway) so the copy stays cheap, plus locally-generated
# Python bytecode (__pycache__/*.pyc, also gitignored) so the fixture depends
# only on committed source, not on whatever a prior local run happened to leave.
_copy_ci_tree() {
    local dest="$1/ci"
    mkdir -p "$dest"
    # Copy everything under ci/ except the runs/ artifacts dir and generated
    # bytecode, via a tar pipe (portable, preserves perms incl. the +x dispatcher).
    tar -C "$REPO_ROOT/ci" \
        --exclude=./runs --exclude='*/__pycache__' --exclude='*.pyc' \
        -cf - . | tar -C "$dest" -xf -
}

setup() {
    [ -x "$QCI" ] || skip "qci not found/executable at $QCI"
    BATS_TMP="$(mktemp -d)"
    # Isolate qci artifacts so the test never writes into the repo's ci/runs.
    export QCI_RUNS_DIR="$BATS_TMP/runs"
    # Default posture: NOT sanctioned. Each test opts in explicitly when needed.
    unset QCI_ALLOW_TEST_EDITS
}

teardown() {
    [ -n "${BATS_TMP:-}" ] && rm -rf "$BATS_TMP"
}

@test "non-protected explicit paths pass (exit 0)" {
    run "$QCI" edit-guard -- src/foo.py docs/bar.md README.md
    [ "$status" -eq 0 ]
    [[ "$output" == *"0 protected"* ]]
}

@test "a protected tests/ edit is flagged and FAILS without opt-in" {
    run "$QCI" edit-guard -- tests/unit/test_x.py
    [ "$status" -ne 0 ]
    [[ "$output" == *"PROTECTED"* ]]
    [[ "$output" == *"tests/unit/test_x.py"* ]]
    [[ "$output" == *"not sanctioned"* || "$output" == *"without --allow-test-edits"* ]]
}

@test "a protected ci/prompts/ edit is flagged and FAILS without opt-in" {
    run "$QCI" edit-guard -- ci/prompts/anti-cheat-guidance.md
    [ "$status" -ne 0 ]
    [[ "$output" == *"PROTECTED"* ]]
    [[ "$output" == *"ci/prompts/anti-cheat-guidance.md"* ]]
}

@test "a protected selinux/ edit is flagged and FAILS without opt-in" {
    run "$QCI" edit-guard -- selinux/tier1/qdistro_tier1.te
    [ "$status" -ne 0 ]
    [[ "$output" == *"PROTECTED"* ]]
    [[ "$output" == *"selinux/tier1/qdistro_tier1.te"* ]]
}

@test "--allow-test-edits sanctions a protected edit (exit 0)" {
    run "$QCI" edit-guard --allow-test-edits -- tests/unit/test_x.py
    [ "$status" -eq 0 ]
    [[ "$output" == *"SANCTIONED"* ]]
}

@test "QCI_ALLOW_TEST_EDITS=1 sanctions a protected edit (exit 0)" {
    QCI_ALLOW_TEST_EDITS=1 run "$QCI" edit-guard -- ci/prompts/anti-cheat-guidance.md
    [ "$status" -eq 0 ]
    [[ "$output" == *"SANCTIONED"* ]]
}

@test "a mixed set with one protected path FAILS (one bad apple)" {
    run "$QCI" edit-guard -- src/ok.py tests/integration/vm/edit-guard.bats docs/x.md
    [ "$status" -ne 0 ]
    [[ "$output" == *"PROTECTED"* ]]
    [[ "$output" == *"edit-guard.bats"* ]]
}

@test "fail-safe: an explicit-but-empty path is indeterminate and does NOT pass" {
    # An explicit path that resolves to nothing (empty string) is an
    # indeterminate request: it must FAIL-SAFE, not silently pass-as-clean.
    # (Contrast: a bare `edit-guard` with no args derives from HEAD, which is
    # a determinate working-tree diff — covered by the git-derived tests.)
    run "$QCI" edit-guard -- ""
    [ "$status" -ne 0 ]
    [[ "$output" == *"indeterminate"* || "$output" == *"no paths supplied"* ]]
}

@test "fail-safe: an uncomputable git diff (bogus ref) FAILS, never pass" {
    run "$QCI" edit-guard --changed-from no-such-ref-deadbeef
    [ "$status" -ne 0 ]
    [[ "$output" == *"fail-safe"* || "$output" == *"could not"* ]]
}

@test "--changed-from with no operand is rejected (not silently HEAD)" {
    run "$QCI" edit-guard --changed-from
    [ "$status" -ne 0 ]
}

@test "--changed-from swallowing another flag is rejected" {
    run "$QCI" edit-guard --changed-from --allow-test-edits
    [ "$status" -ne 0 ]
}

@test "--changed-from swallowing a short flag is rejected" {
    run "$QCI" edit-guard --changed-from -x ci/bin/qci
    [ "$status" -ne 0 ]
}

@test "normalization: ./tests/ and repo-absolute protected paths are caught" {
    run "$QCI" edit-guard -- ./tests/unit/x
    [ "$status" -ne 0 ]
    [[ "$output" == *"PROTECTED"* ]]
    # Repo-absolute form normalizes to repo-relative and is still protected.
    run "$QCI" edit-guard -- "$REPO_ROOT/tests/unit/x"
    [ "$status" -ne 0 ]
    [[ "$output" == *"PROTECTED"* ]]
}

@test "git-derived: an UNTRACKED protected file (newly created) is caught" {
    # A newly *created* protected test is exactly the kind of agent edit the
    # guard must catch — and a plain `git diff` never lists untracked files.
    mkdir -p "$BATS_TMP/repo/tests" "$BATS_TMP/repo/src"
    # The dispatcher SOURCES ci/lib/*.sh, so the throwaway repo needs the
    # whole ci/ tree (not just ci/bin/qci) or sourcing fails and main is
    # undefined (exit 127).
    _copy_ci_tree "$BATS_TMP/repo"
    git -C "$BATS_TMP/repo" init -q
    git -C "$BATS_TMP/repo" config user.email t@t
    git -C "$BATS_TMP/repo" config user.name t
    echo "code" > "$BATS_TMP/repo/src/app.py"
    git -C "$BATS_TMP/repo" add -A
    git -C "$BATS_TMP/repo" commit -qm baseline
    # Create — do NOT add — a protected file.
    echo "sneaky" > "$BATS_TMP/repo/tests/new_test.bats"

    run env QCI_RUNS_DIR="$BATS_TMP/runs4" "$BATS_TMP/repo/ci/bin/qci" edit-guard
    [ "$status" -ne 0 ]
    [[ "$output" == *"PROTECTED"* ]]
    [[ "$output" == *"tests/new_test.bats"* ]]
}

@test "git-derived: a protected working-tree edit is detected vs HEAD" {
    # Build a throwaway git repo with the protected layout, commit a clean
    # baseline, then dirty a protected file and run edit-guard with the
    # default (HEAD) diff. It must detect the protected edit and FAIL.
    git -C "$BATS_TMP" init -q
    git -C "$BATS_TMP" config user.email t@t && git -C "$BATS_TMP" config user.name t
    mkdir -p "$BATS_TMP/repo/tests/unit" "$BATS_TMP/repo/src"
    # Use the real ci/ tree so behavior is identical; the dispatcher sources
    # ci/lib/*.sh, so copying only ci/bin/qci would break (exit 127).
    _copy_ci_tree "$BATS_TMP/repo"
    rm -rf "$BATS_TMP/.git"
    git -C "$BATS_TMP/repo" init -q
    git -C "$BATS_TMP/repo" config user.email t@t
    git -C "$BATS_TMP/repo" config user.name t
    echo "clean" > "$BATS_TMP/repo/tests/unit/test_x.py"
    echo "code" > "$BATS_TMP/repo/src/app.py"
    git -C "$BATS_TMP/repo" add -A
    git -C "$BATS_TMP/repo" commit -qm baseline
    # Dirty a protected file in the working tree.
    echo "tampered" >> "$BATS_TMP/repo/tests/unit/test_x.py"

    run env QCI_RUNS_DIR="$BATS_TMP/runs2" "$BATS_TMP/repo/ci/bin/qci" edit-guard
    [ "$status" -ne 0 ]
    [[ "$output" == *"PROTECTED"* ]]
    [[ "$output" == *"tests/unit/test_x.py"* ]]
}

@test "git-derived: a clean tree (no diff vs HEAD) passes" {
    git -C "$BATS_TMP" init -q
    mkdir -p "$BATS_TMP/repo/src"
    # Whole ci/ tree: the dispatcher sources ci/lib/*.sh (exit 127 otherwise).
    _copy_ci_tree "$BATS_TMP/repo"
    rm -rf "$BATS_TMP/.git"
    git -C "$BATS_TMP/repo" init -q
    git -C "$BATS_TMP/repo" config user.email t@t
    git -C "$BATS_TMP/repo" config user.name t
    echo "code" > "$BATS_TMP/repo/src/app.py"
    git -C "$BATS_TMP/repo" add -A
    git -C "$BATS_TMP/repo" commit -qm baseline

    run env QCI_RUNS_DIR="$BATS_TMP/runs3" "$BATS_TMP/repo/ci/bin/qci" edit-guard
    [ "$status" -eq 0 ]
    [[ "$output" == *"nothing to guard"* || "$output" == *"0 protected"* ]]
}

# Build a throwaway repo whose HEAD is a feature branch forked from an
# integration `main`, with a protected edit *committed* on the branch. Echoes
# the repo path. This is the gap that was invisible before: in the real agent
# flow the edit is COMMITTED before CI grades it, so the old HEAD diff saw
# nothing — only a merge-base-vs-integration diff catches it.
_make_committed_branch_repo() {
    local r="$BATS_TMP/repo"
    mkdir -p "$r/tests/unit" "$r/src"
    # Whole ci/ tree: the dispatcher sources ci/lib/*.sh (exit 127 otherwise).
    _copy_ci_tree "$r"
    git -C "$r" init -q -b main
    git -C "$r" config user.email t@t
    git -C "$r" config user.name t
    echo "clean" > "$r/tests/unit/test_x.py"
    echo "code"  > "$r/src/app.py"
    git -C "$r" add -A
    git -C "$r" commit -qm baseline
    # Fork a feature branch and COMMIT a protected edit on it (HEAD is now the
    # branch tip; the working tree is clean, so a HEAD diff would be empty).
    git -C "$r" checkout -q -b feat/x
    echo "tampered" >> "$r/tests/unit/test_x.py"
    echo "more"     >> "$r/src/app.py"
    git -C "$r" add -A
    git -C "$r" commit -qm "feat work + sneak a test edit"
    printf '%s' "$r"
}

@test "git-derived: a COMMITTED protected edit is caught vs the integration merge-base" {
    local r; r="$(_make_committed_branch_repo)"
    # Sanity: a bare HEAD diff is empty (this is exactly why HEAD was wrong).
    [ -z "$(git -C "$r" diff --name-only HEAD)" ]
    # CI-wired form (no ref, no paths): diffs against merge-base with main and
    # therefore sees the committed protected edit. main resolves -> not fail-safe.
    run env QCI_RUNS_DIR="$BATS_TMP/runsC" "$r/ci/bin/qci" edit-guard
    [ "$status" -ne 0 ]
    [[ "$output" == *"PROTECTED"* ]]
    [[ "$output" == *"tests/unit/test_x.py"* ]]
    [[ "$output" == *"not sanctioned"* || "$output" == *"without --allow-test-edits"* ]]
    # And it must NOT be the fail-safe "no base" path — the base resolved fine.
    [[ "$output" != *"could not resolve an integration base ref"* ]]
}

@test "git-derived: a COMMITTED protected edit is SANCTIONED via QCI_ALLOW_TEST_EDITS" {
    local r; r="$(_make_committed_branch_repo)"
    run env QCI_RUNS_DIR="$BATS_TMP/runsCA" QCI_ALLOW_TEST_EDITS=1 \
        "$r/ci/bin/qci" edit-guard
    [ "$status" -eq 0 ]
    [[ "$output" == *"SANCTIONED"* ]]
}

@test "git-derived: a non-protected committed edit on a branch passes vs merge-base" {
    local r="$BATS_TMP/repo"
    mkdir -p "$r/src"
    # Whole ci/ tree: the dispatcher sources ci/lib/*.sh (exit 127 otherwise).
    _copy_ci_tree "$r"
    git -C "$r" init -q -b main
    git -C "$r" config user.email t@t && git -C "$r" config user.name t
    echo "code" > "$r/src/app.py"
    git -C "$r" add -A && git -C "$r" commit -qm baseline
    git -C "$r" checkout -q -b feat/y
    echo "more" >> "$r/src/app.py"
    git -C "$r" add -A && git -C "$r" commit -qm "product fix"
    run env QCI_RUNS_DIR="$BATS_TMP/runsN" "$r/ci/bin/qci" edit-guard
    [ "$status" -eq 0 ]
    [[ "$output" == *"0 protected"* ]]
}

@test "fail-safe: CI-wired guard with NO resolvable integration base FAILS" {
    # An orphan repo with no main/master and no remote: the merge-base cannot be
    # computed. Diffing against HEAD here would hide a committed protected edit,
    # so the guard must FAIL-SAFE (block), never pass clean.
    local r="$BATS_TMP/repo"
    mkdir -p "$r/tests"
    # Whole ci/ tree: the dispatcher sources ci/lib/*.sh (exit 127 otherwise).
    _copy_ci_tree "$r"
    git -C "$r" init -q -b feature-only
    git -C "$r" config user.email t@t && git -C "$r" config user.name t
    echo "x" > "$r/tests/sneaky.bats"
    git -C "$r" add -A && git -C "$r" commit -qm "committed protected edit on lone branch"
    # Force the candidate list to refs that do not exist in this repo.
    run env QCI_RUNS_DIR="$BATS_TMP/runsFS" QCI_BASE_REF="origin/main main" \
        "$r/ci/bin/qci" edit-guard
    [ "$status" -ne 0 ]
    [[ "$output" == *"could not resolve an integration base ref"* ]]
    [[ "$output" == *"fail-safe"* ]]
}

@test "CI-wired: gate_host invokes the edit-guard (not wired to nothing)" {
    # The original review caught the guard being wired to NOTHING — defined but
    # never called by any gate, so a normal CI run gave zero protection. Assert
    # the host gate (which runs in `qci host` and `qci full`) actually calls
    # gate_edit_guard, and that it is the CI form (empty ref so it diffs against
    # the integration merge-base, not a working-tree-only HEAD diff).
    #
    # Since commit 8e4e0b1 split the runner into ci/lib modules, gate_host()
    # lives in ci/lib/gates/host.sh (no longer inline in the dispatcher), so
    # grep the file that actually defines it.
    local host_gate="$REPO_ROOT/ci/lib/gates/host.sh"
    [ -f "$host_gate" ] || skip "gate_host source not found at $host_gate"
    run bash -c "awk '/^gate_host\\(\\)/{f=1} f{print} /^}/{if(f)exit}' '$host_gate'"
    [ "$status" -eq 0 ]
    [[ "$output" == *"gate_edit_guard"* ]]
    # CI form: called with an empty changed-from ref (\"\"), so the default
    # merge-base-vs-integration derivation runs.
    [[ "$output" == *'gate_edit_guard ""'* ]]
}
