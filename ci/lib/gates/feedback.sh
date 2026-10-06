#!/usr/bin/env bash
# Development feedback, never an acceptance gate. Sourced by qci.
# shellcheck shell=bash

# Consumer map for this single slice. Shared receive/send and installation
# changes need additional consumers; keep them at FULL until reviewed mappings
# can justify a smaller set. This map only reports, never selects execution.
feedback_required_gates() {
    local p
    [ "$#" -gt 0 ] || { echo "$ALL_GATES"; return; }
    for p in "$@"; do
        case "$p" in
            qdfileman/*) ;;
            sdk/*|scripts/install/*|*) echo "$ALL_GATES"; return ;;
        esac
    done
    echo 'host bats gui'
}

gate_feedback() {
    qci_assert_run_dir || return $?
    local component=${1:-} rc required started=$SECONDS saved_timeout=${RUN_STEP_TIMEOUT:-0}
    if [ "$component" != qdfileman ] || [ "${QCI_RELEASE:-0}" = 1 ]; then
        record_blocked feedback "$component" "$EXIT_USAGE" args "requires qdfileman; development feedback cannot be release evidence"
        return "$EXIT_USAGE"
    fi
    shift
    local p
    for p in "$@"; do
        case "$p" in
            ''|/*|-*|../*|*/../*|*/..|*$'\n'*|*$'\t'*)
                record_blocked feedback paths "$EXIT_USAGE" args "expected repo-relative changed paths"
                return "$EXIT_USAGE" ;;
        esac
    done
    kv feedback_acceptance none
    kv feedback_job qdfileman-pytest
    kv feedback_revision "$(git -C "$QDISTRO_REPO" rev-parse HEAD)"
    required=$(feedback_required_gates "$@")
    kv feedback_required_gates "$required"
    printf '%s\n' "$@" > "$RDIR/host/feedback-paths.txt"
    # Content identity includes dirty sources, plus the interpreter and package
    # versions actually visible to the same login shell used by run_logged.
    local deps="$RDIR/host/feedback-dependencies.txt"
    (
        cd "$QDISTRO_REPO" || exit
        printf 'consumer=qdfileman\nsource_dependencies=qdfileman sdk scripts/install ci/lib/gates/host.sh\n'
        find qdfileman sdk scripts/install -type f \
            ! -path '*/__pycache__/*' ! -path '*/.pytest_cache/*' ! -path '*/.git/*' \
            ! -name '*.pyc' -print0 | sort -z | xargs -0 -r sha256sum
        sha256sum ci/lib/gates/host.sh
        bash -lc "$(qci_login_cmd '') "'python3 -c "import sys, importlib.metadata as m; print(sys.executable); print(sys.version); print({p: m.version(p) for p in (\"pytest\", \"PyQt6\", \"pytest-qt\")})"'
    ) > "$deps" 2>&1
    kv feedback_dependencies "host/feedback-dependencies.txt"
    log "development feedback only; acceptance still requires: $required"
    RUN_STEP_TIMEOUT="${QCI_HOST_STEP_TIMEOUT:-600}"
    host_job_qdfileman feedback 'development feedback only; acceptance=none'; rc=$?
    RUN_STEP_TIMEOUT=$saved_timeout
    kv feedback_outcome "$rc"
    kv feedback_time_to_result_s "$((SECONDS - started))"
    kv feedback_jobs_selected 1
    kv feedback_jobs_executed 1
    kv feedback_jobs_skipped 0
    return "$rc"
}
