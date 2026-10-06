#!/usr/bin/env bats

setup() {
    export REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/../../.." && pwd)"
    export QCI_RUNS_DIR="$BATS_TEST_TMPDIR/runs"
}

@test "feedback: release mode refuses before creating a run" {
    run env QCI_RELEASE=1 "$REPO_ROOT/ci/bin/qci" feedback qdfileman
    echo "$output"
    [ "$status" -eq 2 ]
    [[ "$output" == *"forbidden with QCI_RELEASE=1"* ]]
    [ ! -d "$QCI_RUNS_DIR" ]
}

@test "feedback: unsupported or missing component fails without executing a job" {
    for component in '' qdwin; do
        run "$REPO_ROOT/ci/bin/qci" feedback "$component"
        echo "$output"
        [ "$status" -eq 2 ]
    done
    run find "$QCI_RUNS_DIR" -name qdfileman-pytest.log
    [ "$output" = '' ]
}

@test "feedback: conservative consumer mapping keeps shared and unknown changes at full" {
    run bash -c '
        . "$REPO_ROOT/ci/lib/affected.sh"
        . "$REPO_ROOT/ci/lib/gates/feedback.sh"
        [ "$(feedback_required_gates qdfileman/qfileman/window.py)" = "host bats gui" ] || exit 1
        for p in sdk/qdistro_app/app_receiver.py scripts/install/qdistro-bootstrap.sh unknown/path; do
            [ "$(feedback_required_gates qdfileman/qfileman/window.py "$p")" = "$ALL_GATES" ] || exit 1
        done
        [ "$(feedback_required_gates)" = "$ALL_GATES" ]
    '
    echo "$output"
    [ "$status" -eq 0 ]
}

# Run the actual host row implementation and feedback gate while recording each job instead of
# executing native builds. Their shared job must supply exactly the same command,
# working directory and classification; a nonzero result must survive both gates.
@test "feedback: host and development gate share the job and propagate its failure" {
    run bash -c '
        QDISTRO_REPO=$REPO_ROOT WORKSPACE=$REPO_ROOT QCI_DIR=$REPO_ROOT/ci
        . "$QCI_DIR/lib/bootstrap.sh"
        RDIR=$QCI_RUNS_DIR; mkdir -p "$RDIR/host"
        . "$QCI_DIR/lib/core.sh"
        . "$QCI_DIR/lib/affected.sh"
        . "$QCI_DIR/lib/gates/host.sh"
        . "$QCI_DIR/lib/gates/feedback.sh"
        qci_assert_run_dir() { return 0; }
        qci_assert_repo() { return 0; }
        gate_edit_guard() { return 0; }
        gate_selftest() { return 0; }
        record_result() { :; }
        record_skip() { :; }
        coverage_floor_check() { return 0; }
        run_logged() {
            printf "%s\n" "$2" >> "$RDIR/$1-jobs"
            if [ "$2" = qdfileman-pytest ]; then
                printf "%s\n" "$3" "$4" "$5" "$6" > "$RDIR/$1-command"
                return "$EXIT_HOST"
            fi
            return 0
        }
        host_container_rows; host_rc=$?
        gate_feedback qdfileman qdfileman/qfileman/window.py; feedback_rc=$?
        [ "$host_rc" = "$EXIT_HOST" ] && [ "$feedback_rc" = "$EXIT_HOST" ] || exit 1
        diff "$RDIR/host-command" "$RDIR/feedback-command" || exit 1
        [ "$(grep -cx qdfileman-pytest "$RDIR/host-jobs")" = 1 ] || exit 1
        [ "$(cat "$RDIR/feedback-jobs")" = qdfileman-pytest ] || exit 1
        grep -qx qdwin-meson "$RDIR/host-jobs" || exit 1
        grep -qx qdshell-local "$RDIR/host-jobs" || exit 1
        grep -qx qdterm-pytest "$RDIR/host-jobs" || exit 1
        grep -qx "feedback_acceptance=none" "$RDIR/manifest.txt" || exit 1
        grep -qx "feedback_outcome=$EXIT_HOST" "$RDIR/manifest.txt" || exit 1
        grep -qx "feedback_required_gates=host bats gui" "$RDIR/manifest.txt" || exit 1
        grep -qx qdfileman/qfileman/window.py "$RDIR/host/feedback-paths.txt" || exit 1
        grep -q sdk/qdistro_app/app_receiver.py "$RDIR/host/feedback-dependencies.txt"
    '
    echo "$output"
    [ "$status" -eq 0 ]
}
