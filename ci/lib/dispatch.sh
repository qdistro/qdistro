#!/usr/bin/env bash
# qci module: gate_full, triage helpers, main dispatch
# Extracted verbatim from bin/qci. SOURCED by bin/qci into the single
# CI-runner process (shared RDIR/CREATED_VMS/golden state/traps); it is
# NOT executed standalone. See ci/AGENTS.md for the module map.
# shellcheck shell=bash

gate_full() {
    qci_assert_run_dir || return $?
    local rc=$EXIT_OK step_rc
    gate_preflight || return $?
    gate_host; step_rc=$?
    [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc
    gate_release_manifest; step_rc=$?
    [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc
    gate_bootstrap_release_profile; step_rc=$?
    [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc
    gate_vm_smoke ""; step_rc=$?
    [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc
    gate_bats ""; step_rc=$?
    [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc
    gate_gui ""; step_rc=$?
    [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc
    return "$rc"
}

latest_run() {
    find "$RUNS_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' 2>/dev/null | sort -nr | awk 'NR==1 {print $2}'
}

print_triage() {
    local dir=$1
    [ -d "$dir" ] || { echo "no such run: $dir" >&2; return 2; }
    if [ ! -f "$dir/report.md" ]; then
        python3 "$REPORT_PY" "$dir" >/dev/null 2>&1 || true
    fi
    sed -n '1,220p' "$dir/report.md"
    echo
    echo "Artifacts: $dir"
}

main() {
    local cmd=${1:-}
    shift || true
    case "$cmd" in
        -h|--help|help|"") usage; exit "$EXIT_USAGE" ;;
    esac

    case "$cmd" in
        report|triage|list-runs)
            ;;
        *)
            init_run "$cmd" "$@"
            ;;
    esac

    local rc=$EXIT_OK date_arg="" name_arg="" files=() scenarios=() run_dir="" latest=0
    case "$cmd" in
        preflight)
            gate_preflight; rc=$?; finish_run "$rc" ;;
        lint)
            gate_lint; rc=$?; finish_run "$rc" ;;
        selftest)
            gate_selftest; rc=$?; finish_run "$rc" ;;
        image)
            gate_image "$@"; rc=$?; finish_run "$rc" ;;
        registry-check)
            gate_registry_check; rc=$?; finish_run "$rc" ;;
        release-manifest)
            gate_release_manifest; rc=$?; finish_run "$rc" ;;
        bootstrap-release-profile)
            gate_bootstrap_release_profile; rc=$?; finish_run "$rc" ;;
        affected)
            local changed_from="" do_run=0 affected_vm="" affected_paths=()
            while [ $# -gt 0 ]; do
                case "$1" in
                    --changed-from) shift; changed_from=${1:-} ;;
                    --run) do_run=1 ;;
                    --vm) shift; affected_vm=${1:-} ;;
                    --) shift; while [ $# -gt 0 ]; do affected_paths+=("$1"); shift; done; break ;;
                    -*) record_blocked affected "$1" "$EXIT_USAGE" args "unknown affected flag"; finish_run "$EXIT_USAGE" ;;
                    *) affected_paths+=("$1") ;;
                esac
                shift
            done
            gate_affected "$changed_from" "$do_run" "$affected_vm" "${affected_paths[@]}"
            rc=$?; finish_run "$rc" ;;
        edit-guard)
            local eg_changed_from="" eg_allow=0 eg_paths=()
            while [ $# -gt 0 ]; do
                case "$1" in
                    --changed-from)
                        shift
                        # A missing/empty operand (or another flag swallowed as
                        # the ref) is a malformed, indeterminate request —
                        # reject it rather than silently defaulting to HEAD,
                        # which could mask the caller's intent.
                        case "${1:-}" in
                            ""|-*)
                                record_blocked edit-guard "--changed-from" "$EXIT_USAGE" args "--changed-from requires a non-empty, non-flag ref argument"
                                finish_run "$EXIT_USAGE" ;;
                        esac
                        eg_changed_from=$1
                        ;;
                    --allow-test-edits) eg_allow=1 ;;
                    --) shift; while [ $# -gt 0 ]; do eg_paths+=("$1"); shift; done; break ;;
                    -*) record_blocked edit-guard "$1" "$EXIT_USAGE" args "unknown edit-guard flag"; finish_run "$EXIT_USAGE" ;;
                    *) eg_paths+=("$1") ;;
                esac
                shift
            done
            gate_edit_guard "$eg_changed_from" "$eg_allow" "${eg_paths[@]}"
            rc=$?; finish_run "$rc" ;;
        replay)
            local replay_scenario=${1:-} replay_vm=${2:-}
            gate_replay "$replay_scenario" "$replay_vm"; rc=$?; finish_run "$rc" ;;
        host)
            gate_host; rc=$?; finish_run "$rc" ;;
        vm-smoke)
            while [ $# -gt 0 ]; do
                case "$1" in
                    --vm) shift; EXPLICIT_VM=${1:-} ;;
                    *) record_blocked vm-smoke "$1" "$EXIT_USAGE" args "unknown arg"; finish_run "$EXIT_USAGE" ;;
                esac
                shift
            done
            gate_vm_smoke "$EXPLICIT_VM"; rc=$?; finish_run "$rc" ;;
        bats)
            while [ $# -gt 0 ]; do
                case "$1" in
                    --vm) shift; EXPLICIT_VM=${1:-} ;;
                    --file)
                        shift
                        if [ -z "${1:-}" ] || [ ! -f "${1:-}" ]; then
                            record_blocked bats "${1:-<missing>}" "$EXIT_USAGE" args "--file requires an existing .bats file"
                            finish_run "$EXIT_USAGE"
                        fi
                        files+=("$1")
                        ;;
                    *) files+=("$1") ;;
                esac
                shift
            done
            gate_bats "$EXPLICIT_VM" "${files[@]}"; rc=$?; finish_run "$rc" ;;
        gui)
            while [ $# -gt 0 ]; do
                case "$1" in
                    --vm) shift; EXPLICIT_VM=${1:-} ;;
                    --scenario) shift; scenarios+=("${1:-}") ;;
                    --no-require-agent) export QCI_REQUIRE_AGENT_GUI=0 ;;
                    *) record_blocked gui "$1" "$EXIT_USAGE" args "unknown arg"; finish_run "$EXIT_USAGE" ;;
                esac
                shift
            done
            if [ "${#scenarios[@]}" -gt 0 ]; then
                agent_scenarios() { printf '%s\n' "${scenarios[@]}"; }
            fi
            gate_gui "$EXPLICIT_VM"; rc=$?; finish_run "$rc" ;;
        full)
            while [ $# -gt 0 ]; do
                case "$1" in
                    --keep-on-fail) export QCI_KEEP_FAILED_VM=1 ;;
                    --delete-failed-vm) export QCI_DELETE_FAILED_VM=1 ;;
                    --no-require-agent) export QCI_REQUIRE_AGENT_GUI=0 ;;
                    *) record_blocked full "$1" "$EXIT_USAGE" args "unknown arg"; finish_run "$EXIT_USAGE" ;;
                esac
                shift
            done
            gate_full; rc=$?; finish_run "$rc" ;;
        snapshot-daily)
            while [ $# -gt 0 ]; do
                case "$1" in
                    --date) shift; date_arg=${1:-} ;;
                    --name) shift; name_arg=${1:-} ;;
                    *) record_blocked snapshot-daily "$1" "$EXIT_USAGE" args "unknown arg"; finish_run "$EXIT_USAGE" ;;
                esac
                shift
            done
            gate_snapshot_daily "$date_arg" "$name_arg"; rc=$?; finish_run "$rc" ;;
        cleanup)
            gate_cleanup "$@"; rc=$?; finish_run "$rc" ;;
        report)
            while [ $# -gt 0 ]; do
                case "$1" in
                    --latest) latest=1 ;;
                    --run) shift; run_dir=${1:-} ;;
                    *) echo "unknown arg: $1" >&2; exit "$EXIT_USAGE" ;;
                esac
                shift
            done
            [ "$latest" = 1 ] && run_dir=$(latest_run)
            [ -n "$run_dir" ] || { echo "set --latest or --run" >&2; exit "$EXIT_USAGE"; }
            python3 "$REPORT_PY" "$run_dir"
            ;;
        triage)
            while [ $# -gt 0 ]; do
                case "$1" in
                    --latest) latest=1 ;;
                    --run) shift; run_dir=${1:-} ;;
                    *) run_dir=$1 ;;
                esac
                shift
            done
            [ "$latest" = 1 ] && run_dir=$(latest_run)
            [ -n "$run_dir" ] || { echo "set --latest or --run" >&2; exit "$EXIT_USAGE"; }
            print_triage "$run_dir"
            ;;
        list-runs)
            find "$RUNS_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%TY-%Tm-%Td %TH:%TM %p\n' 2>/dev/null | sort
            ;;
        *)
            echo "unknown command: $cmd" >&2
            usage >&2
            exit "$EXIT_USAGE"
            ;;
    esac
}
