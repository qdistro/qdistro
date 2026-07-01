#!/usr/bin/env bats
#
# Host-only test for the per-project <NAME>_REPO export contract
# (ci/lib/bootstrap.sh). GUI scenarios anchor helper `source` paths on
# ${QDWIN_REPO}/... / ${QDISTRO_REPO}/..., so these vars must be EXPORTED (visible
# to the agent's child shell), not merely set. Pins the derivation — including the
# hyphenated-project spelling — so a regression is caught host-side.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    BOOT="$REPO_ROOT/ci/lib/bootstrap.sh"
}

# Source bootstrap.sh with a synthetic WORKSPACE, then echo one var from a CHILD
# shell — proving it was exported (a child inherits only exported vars).
child_val() {
    local var=$1
    WORKSPACE=/ws bash -c "source '$BOOT' >/dev/null 2>&1; bash -c 'printf %s \"\$$var\"'"
}

@test "bootstrap exports WORKSPACE + QDISTRO_REPO to a child shell" {
    [ "$(child_val WORKSPACE)" = "/ws" ]
    [ "$(child_val QDISTRO_REPO)" = "/ws/qdistro" ]
}

@test "bootstrap exports QDWIN_REPO / QDLOCKER_REPO (used by GUI scenarios)" {
    [ "$(child_val QDWIN_REPO)" = "/ws/qdwin" ]
    [ "$(child_val QDLOCKER_REPO)" = "/ws/qdlocker" ]
}

@test "bootstrap derives the hyphenated project spelling correctly" {
    [ "$(child_val QDCHROME_EXTENSION_REPO)" = "/ws/qdchrome-extension" ]
    [ "$(child_val QDFIREFOX_EXTENSION_REPO)" = "/ws/qdfirefox-extension" ]
}

@test "the export is guarded: no WORKSPACE => no bogus /<name> repo paths" {
    # With WORKSPACE unset the block is skipped (no export of QDWIN_REPO=/qdwin).
    run env -u WORKSPACE bash -c "source '$BOOT' >/dev/null 2>&1; printf '[%s]' \"\${QDWIN_REPO:-unset}\""
    [ "$output" = "[unset]" ]
}

@test "a discovered QDISTRO_REPO is NOT clobbered by \$WORKSPACE/qdistro (copied/renamed checkout)" {
    # bin/qci discovers QDISTRO_REPO from the dispatcher's own location and
    # exports it before sourcing bootstrap. When qci runs from a copied/renamed
    # checkout (e.g. the edit-guard throwaway repo at $BATS_TMP/repo) that path
    # is NOT $WORKSPACE/qdistro. The PROJECTS loop must keep the discovered value
    # or the git-derived edit-guard checks query a nonexistent tree.
    run env WORKSPACE=/ws QDISTRO_REPO=/some/copied/repo \
        bash -c "source '$BOOT' >/dev/null 2>&1; bash -c 'printf %s \"\$QDISTRO_REPO\"'"
    [ "$output" = "/some/copied/repo" ]
    # Sibling repos are still derived from WORKSPACE — only qdistro is pinned.
    run env WORKSPACE=/ws QDISTRO_REPO=/some/copied/repo \
        bash -c "source '$BOOT' >/dev/null 2>&1; bash -c 'printf %s \"\$QDWIN_REPO\"'"
    [ "$output" = "/ws/qdwin" ]
}

@test "QDISTRO_REPO is derived from WORKSPACE when not pre-discovered" {
    # Sourced standalone with only WORKSPACE (no discovered QDISTRO_REPO), the
    # loop must still derive QDISTRO_REPO=$WORKSPACE/qdistro so the anchored
    # ${QDISTRO_REPO}/... helper paths resolve.
    [ "$(child_val QDISTRO_REPO)" = "/ws/qdistro" ]
}
