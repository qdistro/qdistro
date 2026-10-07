#!/usr/bin/env bats
#
# Host-only: the GUI agent gets its scenario path in QCI_SCENARIO_FILE and the
# prompt tells it to read the scenario through that variable, never by
# retyping the path.
#
# Why: in full-20261006T175536Z-3524705 the permissions-gui/04 driver retyped
# `.worktrees/verify-3791ee5a8/...` as `.worktrees/verify-3791ee5a/...`, found
# no such directory, and recorded ERROR "scenario file missing" without
# running anything. The artifact dir already travels in QCI_GUI_ARTIFACT_DIR
# for the same reason.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/core.sh"
    source "$REPO_ROOT/ci/lib/gates/gui.sh"
}

@test "prompt names QCI_SCENARIO_FILE and its value" {
    RDIR="$BATS_TEST_TMPDIR/run"; mkdir -p "$RDIR"
    local sc="$BATS_TEST_TMPDIR/wt/tests/integration/permissions-gui/04-x.md"
    write_agent_prompt qci-vm-1 "$sc" "$BATS_TEST_TMPDIR/prompt.txt" \
        "$BATS_TEST_TMPDIR/art" "$BATS_TEST_TMPDIR/scratch" slug
    local p; p=$(cat "$BATS_TEST_TMPDIR/prompt.txt")
    printf '%s\n' "$p" | grep -qF "\`QCI_SCENARIO_FILE=$sc\`"
    printf '%s\n' "$p" | grep -qF -- '- `"$QCI_SCENARIO_FILE"` (= `'"$sc"'`)'
    printf '%s\n' "$p" | grep -qF 'never retype this path'
}

@test "every agent launch exports QCI_SCENARIO_FILE" {
    # Both launch sites (first attempt and fresh-VM retry) set the variable
    # in the same env prefix as QCI_SCENARIO_SLUG.
    local launches exports
    launches=$(grep -c 'run_agent_command "\$prompt"' "$REPO_ROOT/ci/lib/gates/gui.sh")
    exports=$(grep -c 'QCI_SCENARIO_FILE="\$scenario" \\$' "$REPO_ROOT/ci/lib/gates/gui.sh")
    [ "$launches" -ge 2 ]
    [ "$exports" -eq "$launches" ]
}
