#!/usr/bin/env bats
#
# Host-only tests for GUI agent artifact alias + harvest recovery
# (ci/lib/gates/gui.sh::gui_make_artifact_alias / gui_harvest_agent_artifacts).
#
# Reproduces the Luna full-run failure mode: agent writes PASS under a truncated
# run dir (timestamp only, PID dropped) while the harness grades the real
# full-<stamp>-<pid> adir. Also locks concurrent-safety: foreign slugs and
# conflicting verdicts must not be harvested.
#
# NO VM is booted.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/gui.sh"

    TMPHOME="$BATS_TEST_TMPDIR"
    export TMPDIR="$TMPHOME/tmp"
    export QCI_GUI_ART_ALIAS_ROOT="$TMPHOME/art-alias"
    mkdir -p "$TMPDIR" "$QCI_GUI_ART_ALIAS_ROOT"

    export RUNS_DIR="$TMPHOME/runs"
    export RDIR="$RUNS_DIR/full-20260806T173303Z-193313"
    mkdir -p "$RDIR/gui"
}

@test "alias: short path is a symlink to the canonical adir" {
    adir="$RDIR/gui/qdlocker_tests_gui_02-fprintd-fallback.md"
    mkdir -p "$adir"
    run gui_make_artifact_alias "$adir"
    [ "$status" -eq 0 ]
    alias_path=$output
    [[ "$alias_path" == "$QCI_GUI_ART_ALIAS_ROOT/"* ]]
    [ -L "$alias_path" ]
    # writes through the alias land in the canonical dir
    printf 'PASS\n' > "$alias_path/status.txt"
    [ -f "$adir/status.txt" ]
    [ "$(cat "$adir/status.txt")" = PASS ]
}

@test "alias: distinct adirs get distinct keys (concurrency-safe)" {
    a1="$RDIR/gui/scenario-one.md"
    a2="$RDIR/gui/scenario-two.md"
    mkdir -p "$a1" "$a2"
    s1=$(gui_make_artifact_alias "$a1")
    s2=$(gui_make_artifact_alias "$a2")
    [ "$s1" != "$s2" ]
    printf 'PASS\n' > "$s1/status.txt"
    printf 'FAIL\n' > "$s2/status.txt"
    [ "$(cat "$a1/status.txt")" = PASS ]
    [ "$(cat "$a2/status.txt")" = FAIL ]
}

@test "harvest: recovers status from truncated run-dir (Luna pid-drop)" {
    slug="qdlocker_tests_gui_02-fprintd-fallback.md"
    adir="$RDIR/gui/$slug"
    mkdir -p "$adir"
    # Misplaced evidence (agent dropped -193313)
    wrong="$RUNS_DIR/full-20260806T173303Z/gui/$slug"
    mkdir -p "$wrong"
    printf 'PASS\n' > "$wrong/status.txt"
    printf '# report — PASS\n' > "$wrong/report.md"
    printf 'x' > "$wrong/step1-locked.png"

    log="$adir.agent.log"
    printf 'ART=%s\nstatus written\n' "$wrong" > "$log"

    gui_harvest_agent_artifacts "$adir" "$slug" "$log"

    [ -f "$adir/status.txt" ]
    [ "$(cat "$adir/status.txt")" = PASS ]
    [ -f "$adir/report.md" ]
    [ -f "$adir/step1-locked.png" ]
    [ -f "$adir/.harvested-from" ]
    grep -q "harvested_from=$wrong" "$adir/.harvested-from"
    # grading sees PASS
    [ "$(agent_artifact_status "$adir" "$log")" = PASS ]
}

@test "harvest: no-op when canonical status already present" {
    slug="already-good.md"
    adir="$RDIR/gui/$slug"
    mkdir -p "$adir"
    printf 'FAIL\n' > "$adir/status.txt"
    wrong="$RUNS_DIR/full-20260806T173303Z/gui/$slug"
    mkdir -p "$wrong"
    printf 'PASS\n' > "$wrong/status.txt"

    gui_harvest_agent_artifacts "$adir" "$slug" "/dev/null"
    # must not overwrite the real FAIL with the misplaced PASS
    [ "$(cat "$adir/status.txt")" = FAIL ]
    [ ! -f "$adir/.harvested-from" ]
}

@test "harvest: ignores foreign slug under concurrent load" {
    slug="mine.md"
    other="other-scenario.md"
    adir="$RDIR/gui/$slug"
    mkdir -p "$adir"
    foreign="$RUNS_DIR/full-20260806T173303Z/gui/$other"
    mkdir -p "$foreign"
    printf 'PASS\n' > "$foreign/status.txt"

    gui_harvest_agent_artifacts "$adir" "$slug" "/dev/null"
    [ ! -f "$adir/status.txt" ]
    [ "$(agent_artifact_status "$adir" "/dev/null")" = UNKNOWN ]
}

@test "harvest: refuses conflicting misplaced verdicts" {
    slug="conflict.md"
    adir="$RDIR/gui/$slug"
    mkdir -p "$adir"
    w1="$RUNS_DIR/run-a/gui/$slug"
    w2="$RUNS_DIR/run-b/gui/$slug"
    mkdir -p "$w1" "$w2"
    printf 'PASS\n' > "$w1/status.txt"
    printf 'FAIL\n' > "$w2/status.txt"

    # Neither is under RDIR / truncated sibling, so unanimous-verdict check applies
    gui_harvest_agent_artifacts "$adir" "$slug" "/dev/null"
    [ ! -f "$adir/status.txt" ]
}

@test "harvest: prefers truncated sibling of this RDIR over unrelated runs" {
    slug="prefer.md"
    adir="$RDIR/gui/$slug"
    mkdir -p "$adir"
    # Unrelated older run says FAIL
    other="$RUNS_DIR/full-19990101T000000Z-1/gui/$slug"
    mkdir -p "$other"
    printf 'FAIL\n' > "$other/status.txt"
    # Truncated sibling of our RDIR says PASS (the real agent write)
    trunc="$RUNS_DIR/full-20260806T173303Z/gui/$slug"
    mkdir -p "$trunc"
    printf 'PASS\n' > "$trunc/status.txt"

    gui_harvest_agent_artifacts "$adir" "$slug" "/dev/null"
    [ "$(cat "$adir/status.txt")" = PASS ]
}

@test "status file verdict: normalizes PASSn typo" {
    f="$TMPHOME/status.txt"
    printf 'PASSn' > "$f"
    [ "$(gui_status_file_verdict "$f")" = PASS ]
}
