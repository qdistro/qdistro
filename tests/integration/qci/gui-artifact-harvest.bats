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

@test "alias: short path is a real directory harvested to canonical adir" {
    adir="$RDIR/gui/qdlocker_tests_gui_02-fprintd-fallback.md"
    mkdir -p "$adir"
    run gui_make_artifact_alias "$adir"
    [ "$status" -eq 0 ]
    alias_path=$output
    [[ "$alias_path" == "$QCI_GUI_ART_ALIAS_ROOT/"* ]]
    [ -d "$alias_path" ]
    [ ! -L "$alias_path" ]
    [ "$(stat -c %a "$QCI_GUI_ART_ALIAS_ROOT")" = 700 ]
    [ "$(stat -c %a "$alias_path")" = 700 ]
    # Hardened ImageMagick rejects symlinked output paths, so writes stay in the
    # real alias until the harness harvests them.
    printf 'PASS\n' > "$alias_path/status.txt"
    printf '[evidence](%s/step.png)\n' "$alias_path" > "$alias_path/report.md"
    printf 'pixels\n' > "$alias_path/step.png"
    [ ! -f "$adir/status.txt" ]
    gui_harvest_agent_artifacts "$adir" \
        qdlocker_tests_gui_02-fprintd-fallback.md /dev/null "$alias_path"
    [ -f "$adir/status.txt" ]
    [ "$(cat "$adir/status.txt")" = PASS ]
    grep -Fq '[evidence](./step.png)' "$adir/report.md"
    ! grep -Fq "$alias_path" "$adir/report.md"
    grep -q "harvested_from=$alias_path" "$adir/.harvested-from"
    [ ! -e "$alias_path" ]
    [ ! -e "$alias_path.target" ]
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
    gui_harvest_agent_artifacts "$a1" scenario-one.md /dev/null "$s1"
    gui_harvest_agent_artifacts "$a2" scenario-two.md /dev/null "$s2"
    [ "$(cat "$a1/status.txt")" = PASS ]
    [ "$(cat "$a2/status.txt")" = FAIL ]
}

@test "alias: refuses a historical symlink and sidecar without touching target" {
    adir="$RDIR/gui/migrate.md"
    mkdir -p "$adir"
    key=$(printf '%s' "$adir" | sha256sum | awk '{print substr($1,1,16)}')
    old="$QCI_GUI_ART_ALIAS_ROOT/$key"
    ln -s "$adir" "$old"
    printf '%s\n' "$adir" > "$old.target"
    run gui_make_artifact_alias "$adir"
    [ "$status" -ne 0 ]
    [ -L "$old" ]
    [ "$(cat "$old.target")" = "$adir" ]
    [ -d "$adir" ]
}

@test "alias: sidecar creation failure is fail-closed and leaves no directory" {
    adir="$RDIR/gui/sidecar-fail.md"
    mkdir -p "$adir"
    key=$(printf '%s' "$adir" | sha256sum | awk '{print substr($1,1,16)}')
    alias_path="$QCI_GUI_ART_ALIAS_ROOT/$key"
    mkdir "$alias_path.target"

    run gui_make_artifact_alias "$adir"
    [ "$status" -ne 0 ]
    [ ! -e "$alias_path" ]
    [ -d "$alias_path.target" ]
}

@test "alias: refuses a stale real directory instead of reusing its verdict" {
    adir="$RDIR/gui/stale-real.md"
    mkdir -p "$adir"
    key=$(printf '%s' "$adir" | sha256sum | awk '{print substr($1,1,16)}')
    alias_path="$QCI_GUI_ART_ALIAS_ROOT/$key"
    mkdir "$alias_path"
    printf 'PASS\n' > "$alias_path/status.txt"

    run gui_make_artifact_alias "$adir"
    [ "$status" -ne 0 ]
    [ "$(cat "$alias_path/status.txt")" = PASS ]
}

@test "alias: refuses a symlinked base and does not touch its target" {
    adir="$RDIR/gui/base-symlink.md"
    foreign="$TMPHOME/foreign-base"
    mkdir -p "$adir" "$foreign"
    rmdir "$QCI_GUI_ART_ALIAS_ROOT"
    ln -s "$foreign" "$QCI_GUI_ART_ALIAS_ROOT"

    run gui_make_artifact_alias "$adir"
    [ "$status" -ne 0 ]
    [ -z "$(find "$foreign" -mindepth 1 -maxdepth 1 -print -quit)" ]
}

@test "alias: refuses a precreated sidecar symlink" {
    adir="$RDIR/gui/sidecar-symlink.md"
    victim="$TMPHOME/victim"
    printf 'do not overwrite\n' > "$victim"
    mkdir -p "$adir"
    key=$(printf '%s' "$adir" | sha256sum | awk '{print substr($1,1,16)}')
    alias_path="$QCI_GUI_ART_ALIAS_ROOT/$key"
    ln -s "$victim" "$alias_path.target"

    run gui_make_artifact_alias "$adir"
    [ "$status" -ne 0 ]
    [ "$(cat "$victim")" = 'do not overwrite' ]
    [ ! -e "$alias_path" ]
}

@test "harvest: refuses an alias whose sidecar names another attempt" {
    adir="$RDIR/gui/mine.md"
    mkdir -p "$adir"
    alias_path=$(gui_make_artifact_alias "$adir")
    printf 'PASS\n' > "$alias_path/status.txt"
    printf '%s\n' "$RDIR/gui/other.md" > "$alias_path.target"

    gui_harvest_agent_artifacts "$adir" mine.md /dev/null "$alias_path"
    [ ! -f "$adir/status.txt" ]
    [ -d "$alias_path" ]
}

@test "harvest: refuses a leaf symlink substituted after alias creation" {
    adir="$RDIR/gui/symlink-swap.md"
    foreign="$TMPHOME/foreign"
    mkdir -p "$adir" "$foreign"
    alias_path=$(gui_make_artifact_alias "$adir")
    rmdir "$alias_path"
    ln -s "$foreign" "$alias_path"
    printf 'PASS\n' > "$foreign/status.txt"
    printf 'foreign proof\n' > "$foreign/report.txt"

    gui_harvest_agent_artifacts "$adir" symlink-swap.md /dev/null "$alias_path"
    [ ! -f "$adir/status.txt" ]
    [ -f "$adir/.harvest-invalid" ]
    [ -L "$alias_path" ]
    [ -f "$foreign/status.txt" ]
}

@test "harvest: conflicting canonical and alias verdicts fail closed" {
    adir="$RDIR/gui/verdict-conflict.md"
    mkdir -p "$adir"
    alias_path=$(gui_make_artifact_alias "$adir")
    printf 'PASS\n' > "$adir/status.txt"
    printf 'FAIL\n' > "$alias_path/status.txt"

    gui_harvest_agent_artifacts "$adir" verdict-conflict.md /dev/null "$alias_path"
    [ -f "$adir/.harvest-invalid" ]
    [ "$(agent_artifact_status "$adir" /dev/null)" = UNKNOWN ]
    [ -d "$alias_path" ]
}

@test "harvest: canonical report cannot mask authenticated alias failure" {
    adir="$RDIR/gui/report-conflict.md"
    mkdir -p "$adir"
    alias_path=$(gui_make_artifact_alias "$adir")
    printf '# report — PASS\n' > "$adir/report.md"
    printf 'FAIL\n' > "$alias_path/status.txt"

    gui_harvest_agent_artifacts "$adir" report-conflict.md /dev/null "$alias_path"
    [ -f "$adir/.harvest-invalid" ]
    [ "$(agent_artifact_status "$adir" /dev/null)" = UNKNOWN ]
}

@test "harvest: symlinked alias status is rejected and never published" {
    adir="$RDIR/gui/status-symlink.md"
    victim="$TMPHOME/status-victim"
    mkdir -p "$adir"
    printf 'PASS\n' > "$victim"
    alias_path=$(gui_make_artifact_alias "$adir")
    ln -s "$victim" "$alias_path/status.txt"

    gui_harvest_agent_artifacts "$adir" status-symlink.md /dev/null "$alias_path"
    [ -f "$adir/.harvest-invalid" ]
    [ ! -e "$adir/status.txt" ]
    [ "$(agent_artifact_status "$adir" /dev/null)" = UNKNOWN ]
}

@test "harvest: invalid alias verdict cannot be masked by canonical PASS" {
    adir="$RDIR/gui/invalid-alias-verdict.md"
    mkdir -p "$adir"
    alias_path=$(gui_make_artifact_alias "$adir")
    printf 'PASS\n' > "$adir/status.txt"
    printf 'POSS\n' > "$alias_path/status.txt"

    gui_harvest_agent_artifacts "$adir" invalid-alias-verdict.md /dev/null "$alias_path"
    [ -f "$adir/.harvest-invalid" ]
    [ "$(agent_artifact_status "$adir" /dev/null)" = UNKNOWN ]
    [ -d "$alias_path" ]
}

@test "harvest: canonical evidence symlink keeps alias proof and fails closed" {
    adir="$RDIR/gui/canonical-evidence-symlink.md"
    victim="$TMPHOME/canonical-victim"
    mkdir -p "$adir"
    printf 'agent proof\n' > "$victim"
    ln -s "$victim" "$adir/report.txt"
    alias_path=$(gui_make_artifact_alias "$adir")
    printf 'PASS\n' > "$alias_path/status.txt"
    printf 'agent proof\n' > "$alias_path/report.txt"

    gui_harvest_agent_artifacts "$adir" canonical-evidence-symlink.md /dev/null "$alias_path"
    [ -f "$adir/.harvest-invalid" ]
    [ "$(agent_artifact_status "$adir" /dev/null)" = UNKNOWN ]
    [ -L "$adir/report.txt" ]
    [ -d "$alias_path" ]
}

@test "harvest: evidence conflict keeps status unpublished and alias intact" {
    adir="$RDIR/gui/evidence-conflict.md"
    mkdir -p "$adir"
    alias_path=$(gui_make_artifact_alias "$adir")
    printf 'PASS\n' > "$alias_path/status.txt"
    printf 'complete agent evidence\n' > "$alias_path/report.txt"
    printf 'partial conflicting evidence\n' > "$adir/report.txt"

    gui_harvest_agent_artifacts "$adir" evidence-conflict.md /dev/null "$alias_path"
    [ ! -f "$adir/status.txt" ]
    [ -f "$adir/.harvest-invalid" ]
    [ -d "$alias_path" ]
    [ "$(cat "$alias_path/report.txt")" = 'complete agent evidence' ]
    [ "$(cat "$adir/report.txt")" = 'partial conflicting evidence' ]
}

@test "harvest: equal PASS verdicts cannot mask conflicting evidence" {
    adir="$RDIR/gui/equal-pass-evidence-conflict.md"
    mkdir -p "$adir"
    alias_path=$(gui_make_artifact_alias "$adir")
    printf 'PASS\n' > "$adir/status.txt"
    printf 'canonical proof\n' > "$adir/report.txt"
    printf 'PASS\n' > "$alias_path/status.txt"
    printf 'different alias proof\n' > "$alias_path/report.txt"

    gui_harvest_agent_artifacts "$adir" equal-pass-evidence-conflict.md /dev/null "$alias_path"
    [ -f "$adir/.harvest-invalid" ]
    [ "$(agent_artifact_status "$adir" /dev/null)" = UNKNOWN ]
    [ -d "$alias_path" ]
    [ "$(cat "$adir/report.txt")" = 'canonical proof' ]
    [ "$(cat "$alias_path/report.txt")" = 'different alias proof' ]
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
