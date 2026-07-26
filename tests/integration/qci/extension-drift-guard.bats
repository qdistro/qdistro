#!/usr/bin/env bats
#
# Extension contract drift guard — the CI wiring, not just the tracker note.
#
# `qdchrome-extension` and `qdfirefox-extension` carry a BYTE-IDENTICAL
# tests/fixtures/golden-frames.js (the bridge wire-protocol contract) with no
# workspace linking them. Each repo's tests/golden-frames-drift.test.js compares
# the two copies, but when the sibling repo is absent it skips-with-warning —
# so a single-repo run is green without ever verifying the two protocol copies
# agree. `$QDISTRO_REQUIRE_SIBLING=1` turns that absence into a hard failure.
#
# 07-release-checklist.md records that release CI MUST set it. This file makes
# the requirement executable: qci's host gate is the thing that actually ships
# the extensions, so the env must be wired there, and the wiring must survive
# refactors. HOST-ONLY, no VM.
#
# ensures: the cross-repo protocol guard cannot be silently absent at the gate
# that ships the extensions.

setup() {
    SRC_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    HOST_GATE="$SRC_ROOT/ci/lib/gates/host.sh"
    [ -f "$HOST_GATE" ] || { echo "host gate not found at $HOST_GATE" >&2; return 1; }
    WORKSPACE_DIR="$(dirname "$SRC_ROOT")"
    CHROME_REPO="${QDCHROME_EXTENSION_REPO:-$WORKSPACE_DIR/qdchrome-extension}"
    FIREFOX_REPO="${QDFIREFOX_EXTENSION_REPO:-$WORKSPACE_DIR/qdfirefox-extension}"
}

# Non-comment lines of the host gate (the explanatory header names the env
# vars too; only live code should satisfy these assertions).
gate_code() {
    grep -vE '^[[:space:]]*#' "$HOST_GATE"
}

@test "host gate: extension steps set QDISTRO_REQUIRE_SIBLING=1" {
    run gate_code
    [ "$status" -eq 0 ]
    [[ "$output" == *"QDISTRO_REQUIRE_SIBLING=1"* ]] || {
        echo "ci/lib/gates/host.sh no longer exports QDISTRO_REQUIRE_SIBLING=1;" >&2
        echo "the cross-repo golden-frames guard would skip-with-warning." >&2
        return 1
    }
}

@test "host gate: sibling fixture path is pinned from \$WORKSPACE (not the ../.. default)" {
    run gate_code
    [ "$status" -eq 0 ]
    [[ "$output" == *"QDISTRO_SIBLING_GOLDEN"* ]] || return 1
    # Both directions must be wired: chrome compares against firefox and
    # vice-versa, anchored at $WORKSPACE so a worktree checkout cannot pick up
    # a stale sibling worktree under .worktrees/.
    [[ "$output" == *'_ext_drift_env qdfirefox-extension'* ]] || return 1
    [[ "$output" == *'_ext_drift_env qdchrome-extension'* ]] || return 1
    [[ "$output" == *'$WORKSPACE/$1/tests/fixtures/golden-frames.js'* ]] || return 1
}

@test "golden frames: the two repo copies are byte-identical" {
    [ -f "$CHROME_REPO/tests/fixtures/golden-frames.js" ] \
        || skip "qdchrome-extension not checked out at $CHROME_REPO"
    [ -f "$FIREFOX_REPO/tests/fixtures/golden-frames.js" ] \
        || skip "qdfirefox-extension not checked out at $FIREFOX_REPO"
    cmp "$CHROME_REPO/tests/fixtures/golden-frames.js" \
        "$FIREFOX_REPO/tests/fixtures/golden-frames.js"
}

@test "drift test: QDISTRO_REQUIRE_SIBLING makes a missing sibling FATAL" {
    # Behavioural (not textual) proof that the env var the gate exports does
    # what the gate relies on: point it at a nonexistent sibling and the drift
    # test must FAIL rather than warn-pass.
    [ -x "$CHROME_REPO/node_modules/.bin/vitest" ] \
        || skip "vitest not installed in $CHROME_REPO (npm ci first)"
    cd "$CHROME_REPO"
    QDISTRO_REQUIRE_SIBLING=1 \
    QDISTRO_SIBLING_GOLDEN="$BATS_TEST_TMPDIR/absent/golden-frames.js" \
        run ./node_modules/.bin/vitest run tests/golden-frames-drift.test.js
    [ "$status" -ne 0 ] || {
        echo "drift test passed with QDISTRO_REQUIRE_SIBLING=1 and no sibling" >&2
        echo "$output" >&2
        return 1
    }
    # Negative control: without the env var the same run warn-passes.
    QDISTRO_SIBLING_GOLDEN="$BATS_TEST_TMPDIR/absent/golden-frames.js" \
        run ./node_modules/.bin/vitest run tests/golden-frames-drift.test.js
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
}
