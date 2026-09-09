#!/usr/bin/env bats
#
# Host-only test for the bats-gate scheduled-basename uniqueness assert
# (ci/lib/gates/bats.sh, H10). Result rows + per-file VM names key on the
# basename, so a duplicate in the SCHEDULED set would silently collide. The pure
# assert_unique_bats_basenames helper takes path args and fails on any duplicate
# basename, naming both paths. Uses a tempdir fixture; no VM, no libvirt.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    TMP="$(mktemp -d)"
    mkdir -p "$TMP/vm" "$TMP/other"
    # bootstrap.sh supplies PROJECTS and project_root, which bats_discover_files
    # now iterates instead of globbing "$WORKSPACE"/*.
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/bootstrap.sh"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/bats.sh"
}

teardown() { [ -n "${TMP:-}" ] && rm -rf "$TMP"; }

@test "unique basenames pass" {
    : > "$TMP/vm/a.bats"; : > "$TMP/vm/b.bats"; : > "$TMP/vm/c.bats"
    run assert_unique_bats_basenames "$TMP/vm/a.bats" "$TMP/vm/b.bats" "$TMP/vm/c.bats"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "empty schedule passes" {
    run assert_unique_bats_basenames
    [ "$status" -eq 0 ]
}

@test "duplicate basename across dirs fails, naming both paths" {
    : > "$TMP/vm/dup.bats"; : > "$TMP/other/dup.bats"
    run assert_unique_bats_basenames "$TMP/vm/dup.bats" "$TMP/other/dup.bats"
    [ "$status" -ne 0 ]
    [[ "$output" == *"duplicate scheduled bats basename dup.bats"* ]]
    [[ "$output" == *"$TMP/vm/dup.bats"* ]]
    [[ "$output" == *"$TMP/other/dup.bats"* ]]
}

@test "the real cross-dir dup is only a problem if BOTH are scheduled" {
    # Mirrors the real repo dup: only the vm/ copy is scheduled today -> passes.
    : > "$TMP/vm/backup-rehearse-e2e.bats"
    run assert_unique_bats_basenames "$TMP/vm/backup-rehearse-e2e.bats" "$TMP/vm/a.bats"
    [ "$status" -eq 0 ]
    # If discovery is ever widened to include the sibling copy too -> fails.
    : > "$TMP/other/backup-rehearse-e2e.bats"
    run assert_unique_bats_basenames \
        "$TMP/vm/backup-rehearse-e2e.bats" "$TMP/other/backup-rehearse-e2e.bats"
    [ "$status" -ne 0 ]
    [[ "$output" == *"backup-rehearse-e2e.bats"* ]]
}

@test "discover includes sibling-repo vm bats and skips the qdistro duplicate" {
    mkdir -p "$TMP/ws/qdistro/tests/integration/vm" \
             "$TMP/ws/qdbrowser/tests/integration/vm"
    : > "$TMP/ws/qdistro/tests/integration/vm/shell-modules.bats"
    : > "$TMP/ws/qdbrowser/tests/integration/vm/qdbrowser-smoke.bats"
    QDISTRO_REPO="$TMP/ws/qdistro" WORKSPACE="$TMP/ws" \
        run bats_discover_files
    [ "$status" -eq 0 ]
    [[ "$output" == *"/qdistro/tests/integration/vm/shell-modules.bats"* ]]
    [[ "$output" == *"/qdbrowser/tests/integration/vm/qdbrowser-smoke.bats"* ]]
    # qdistro's own dir is not listed twice
    count=$(printf '%s\n' "$output" | grep -c 'shell-modules.bats' || true)
    [ "$count" -eq 1 ]
}

@test "discover ignores a stray checkout beside the workspace" {
    # The 2026-09-08 incident: a review checkout named qdistro-ci-host-20260908
    # sat beside the canonical tree, the "$WORKSPACE"/*/tests/integration/vm glob
    # scheduled it, every basename collided, and the uniqueness guard failed the
    # ENTIRE bats gate. A stray with UNIQUE basenames would instead have added
    # unintended tests silently. Only declared PROJECTS are scheduled now.
    mkdir -p "$TMP/ws/qdistro/tests/integration/vm" \
             "$TMP/ws/qdistro-ci-host-20260908/tests/integration/vm" \
             "$TMP/ws/some-review-copy/tests/integration/vm"
    : > "$TMP/ws/qdistro/tests/integration/vm/shell-modules.bats"
    : > "$TMP/ws/qdistro-ci-host-20260908/tests/integration/vm/shell-modules.bats"
    : > "$TMP/ws/some-review-copy/tests/integration/vm/only-here.bats"
    QDISTRO_REPO="$TMP/ws/qdistro" WORKSPACE="$TMP/ws" \
        run bats_discover_files
    [ "$status" -eq 0 ]
    [[ "$output" == *"/ws/qdistro/tests/integration/vm/shell-modules.bats"* ]]
    [[ "$output" != *"qdistro-ci-host-20260908"* ]]
    [[ "$output" != *"only-here.bats"* ]]
    # ...and the whole set still passes the uniqueness guard, which the glob did not.
    mapfile -t files < <(printf '%s\n' "$output")
    run assert_unique_bats_basenames "${files[@]}"
    [ "$status" -eq 0 ]
}

@test "discover honours an explicitly opted-in extra root" {
    # A deliberate side-by-side worktree is still runnable — explicitly.
    mkdir -p "$TMP/ws/qdistro/tests/integration/vm" \
             "$TMP/extra/tests/integration/vm"
    : > "$TMP/ws/qdistro/tests/integration/vm/shell-modules.bats"
    : > "$TMP/extra/tests/integration/vm/extra-suite.bats"
    QDISTRO_REPO="$TMP/ws/qdistro" WORKSPACE="$TMP/ws" \
        QCI_EXTRA_BATS_ROOTS="$TMP/extra" run bats_discover_files
    [ "$status" -eq 0 ]
    [[ "$output" == *"extra-suite.bats"* ]]
}

@test "discover follows a RENAMED qdistro checkout, not the canonical sibling" {
    # QDISTRO_REPO is authoritative: qci run from a renamed/copied checkout must
    # schedule ITS suites, never the canonical tree's next door.
    mkdir -p "$TMP/ws/qdistro/tests/integration/vm" \
             "$TMP/ws/qdistro-work/tests/integration/vm"
    : > "$TMP/ws/qdistro/tests/integration/vm/canonical-only.bats"
    : > "$TMP/ws/qdistro-work/tests/integration/vm/renamed-only.bats"
    QDISTRO_REPO="$TMP/ws/qdistro-work" WORKSPACE="$TMP/ws" \
        run bats_discover_files
    [ "$status" -eq 0 ]
    [[ "$output" == *"renamed-only.bats"* ]]
    [[ "$output" != *"canonical-only.bats"* ]]
}
