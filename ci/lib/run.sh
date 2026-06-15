#!/usr/bin/env bash
# qci module: run lifecycle, result recording, traps
# Extracted verbatim from bin/qci. SOURCED by bin/qci into the single
# CI-runner process (shared RDIR/CREATED_VMS/golden state/traps); it is
# NOT executed standalone. See ci/AGENTS.md for the module map.
# shellcheck shell=bash

init_run() {
    GATE=$1
    shift || true
    mkdir -p "$RUNS_DIR"
    local rid="${GATE}-$(stamp)-$$"
    RDIR="$RUNS_DIR/$rid"
    mkdir -p "$RDIR"/{agent-notes,preflight,host,vm,gui,bats,repos,screenshots,journals,reports,selftest}
    {
        printf 'run_id=%s\n' "$rid"
        printf 'gate=%s\n' "$GATE"
        printf 'started_utc=%s\n' "$(now_utc)"
        printf 'host=%s\n' "$(hostname -s 2>/dev/null || hostname)"
        printf 'user=%s\n' "$(id -un)"
        printf 'workspace=%s\n' "$WORKSPACE"
        printf 'qci=%s\n' "$SELF"
        printf 'command=%q %q' "$SELF" "$GATE"
        local arg
        for arg in "$@"; do
            printf ' %q' "$arg"
        done
        printf '\n'
    } > "$RDIR/manifest.txt"
    printf 'gate\tsubject\tstatus\texit_code\texit_class\tkind\tlog\tnotes\tcategory\n' > "$RDIR/results.tsv"
    printf 'repo\tbranch\thead\tdirty_files\tstatus_log\n' > "$RDIR/repo-state.tsv"
    printf 'gate\tsubject\tprovision_s\twork_s\ttotal_s\trc\tvm\n' > "$RDIR/timings.tsv"
    : > "$RDIR/vm/created-vms.txt"
    collect_repo_state
    record_offline_source
    log "artifacts -> $RDIR"
}

# When QCI_OFFLINE=1, capture the qdistro source tree as a tarball + sha256 in
# the run dir and record both (plus the offline posture) in manifest.txt, so a
# run is reproducible without re-fetching from any network. Cheap no-op when
# offline mode is not requested.
record_offline_source() {
    kv qci_offline "$QCI_OFFLINE"
    [ "$QCI_OFFLINE" = 1 ] || return 0
    local tarball="$RDIR/source.tar.gz" head
    head=$(git -C "$QDISTRO_REPO" rev-parse HEAD 2>/dev/null || echo unknown)
    # Archive the committed tree at HEAD (deterministic; excludes runs/ etc.).
    if git -C "$QDISTRO_REPO" archive --format=tar.gz -o "$tarball" HEAD 2>/dev/null; then
        local sum
        sum=$(sha256sum "$tarball" 2>/dev/null | awk '{print $1}')
        kv source_tarball "$(rel_path "$tarball")"
        kv source_sha256 "${sum:-unknown}"
        kv source_head "$head"
        log "offline: source tarball $(rel_path "$tarball") sha256=${sum:-unknown}"
    else
        kv source_tarball "unavailable (git archive failed)"
        kv source_head "$head"
        log "offline: git archive failed; source tarball not captured"
    fi
}

finish_run() {
    local rc=$1
    FINALIZING=1
    [ -n "$RDIR" ] || exit "$rc"
    # Reclaim per-run golden backing disks (kept if a failed worker was preserved).
    cleanup_run_goldens
    # Release-profile escalation (QCI_RELEASE=1): a `blocked` row in a
    # release-relevant gate means the release was not actually exercised — fail
    # closed. Only escalates a run that otherwise PASSED (rc==0); a real failure
    # already carries its own exit class and takes precedence. The offending
    # rows are recorded so the report shows WHY.
    if [ "${QCI_RELEASE:-0}" = 1 ] && [ -f "$RDIR/results.tsv" ]; then
        local blocked_rows
        blocked_rows=$(awk -F'\t' -v g="$QCI_RELEASE_FATAL_GATES" '
            BEGIN { n = split(g, a, " "); for (i = 1; i <= n; i++) fatal[a[i]] = 1 }
            NR > 1 && $3 == "blocked" && ($1 in fatal) { print $1 "/" $2 }
        ' "$RDIR/results.tsv")
        if [ -n "$blocked_rows" ]; then
            local blocked_count
            blocked_count=$(printf '%s\n' "$blocked_rows" | grep -c .)
            record_result release-profile blocked-rows fail "$EXIT_RELEASE" release release "" \
                "QCI_RELEASE=1: $blocked_count blocked row(s) in release-relevant gates are fatal: $(printf '%s' "$blocked_rows" | tr '\n' ' ')"
            log "release-profile: $blocked_count blocked row(s) fatal under QCI_RELEASE=1"
            [ "$rc" -eq 0 ] && rc=$EXIT_RELEASE
        fi
    fi
    kv finished_utc "$(now_utc)"
    kv exit_code "$rc"
    kv exit_class "$(exit_class_name "$rc")"
    printf '%s\n' "$rc" > "$RDIR/exit-code.txt"
    if [ -x "$REPORT_PY" ] || [ -f "$REPORT_PY" ]; then
        python3 "$REPORT_PY" "$RDIR" > "$RDIR/reports/report-generator.log" 2>&1 || {
            log "report generation failed; see $RDIR/reports/report-generator.log"
        }
    fi
    log "run $(basename "$RDIR") exit=$rc class=$(exit_class_name "$rc")"
    if [ -f "$RDIR/report.md" ]; then
        log "report: $RDIR/report.md"
    fi
    if [ -f "$RDIR/report.html" ]; then
        log "html:   $RDIR/report.html"
    fi
    exit "$rc"
}

abort_run() {
    local sig=$1
    [ "$FINALIZING" = 1 ] && exit "$EXIT_RUNNER"
    log "received $sig; preserving created VMs and finalizing run"
    local vm
    if [ -n "$RDIR" ] && [ -f "$RDIR/vm/created-vms.txt" ]; then
        while IFS= read -r vm; do
            [ -n "$vm" ] && release_vm "$vm" "$EXIT_RUNNER"
        done < "$RDIR/vm/created-vms.txt"
        # Reap any golden still MID-BUILD (domain alive) — an interrupted golden
        # is useless. Promoted goldens have no domain and are reclaimed by
        # cleanup_run_goldens inside finish_run (honouring GOLDEN_PRESERVE).
        local g gd
        for g in "${GOLDEN_INFLIGHT_VMS[@]:-}"; do
            [ -n "$g" ] && "${VIRSH[@]}" dominfo "$g" >/dev/null 2>&1 || continue
            gd=$(vm_disk_path "$g" 2>/dev/null || true)
            "${VIRSH[@]}" destroy "$g" >/dev/null 2>&1 || true
            "${VIRSH[@]}" undefine "$g" --nvram >/dev/null 2>&1 || "${VIRSH[@]}" undefine "$g" >/dev/null 2>&1 || true
            [ -n "$gd" ] && safe_rm_overlay "$gd" >/dev/null 2>&1 || true
        done
        record_result lifecycle "$sig" fail "$EXIT_RUNNER" runner signal "" "run interrupted"
        finish_run "$EXIT_RUNNER"
    fi
    exit "$EXIT_RUNNER"
}

trap 'abort_run INT' INT
trap 'abort_run TERM' TERM
# SIGHUP: a closed terminal/SSH session must not kill the run mid-step and leak
# VMs. Finalize cleanly instead (also see qci-tmux, which decouples from the tty).
trap 'abort_run HUP' HUP

# Map a result's gate + kind to the shared confidence-taxonomy category
# documented in ci/TAXONOMY.md. This is an HONEST, coarse classification of
# *what kind of evidence a row carries* — it adds no gate and changes no
# pass/fail. The 9th `category` column is purely informational so report.md
# can group counts/runtime/skips by confidence band. Unknown shapes fall back
# to "unit" (the safe, lowest-claim bucket) rather than guessing higher.
category_for() {
    local gate=$1 kind=$2
    case "$kind" in
        vm|vm_provision|vm_boot|service) echo vm; return ;;
        bats)                            echo integration; return ;;
        gui|vision|image)                echo gui; return ;;
    esac
    case "$gate" in
        bats)                            echo integration; return ;;
        vm-smoke|snapshot-daily)         echo vm; return ;;
        gui)                             echo gui; return ;;
        host)                            echo unit; return ;;
        preflight|selftest|affected|registry-check|edit-guard|cleanup|lint|lifecycle)
                                         echo unit; return ;;
    esac
    echo unit
}

record_result() {
    local gate=$1 subject=$2 status=$3 exit_code=$4 exit_class=$5 kind=$6 log_path=${7:-} notes=${8:-} category=${9:-}
    notes=${notes//$'\t'/ }
    notes=${notes//$'\n'/ }
    # Derive the taxonomy category when the caller did not pass one (all
    # current callers rely on this default; an explicit 9th arg can override
    # for a row whose confidence differs from its gate/kind, e.g. a
    # source_invariant or fake_backend suite).
    [ -n "$category" ] || category=$(category_for "$gate" "$kind")
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$gate" "$subject" "$status" "$exit_code" "$exit_class" "$kind" \
        "$(rel_path "$log_path")" "$notes" "$category" >> "$RDIR/results.tsv"
}

collect_repo_state() {
    local project repo branch head dirty status_log
    for project in "${PROJECTS[@]}"; do
        repo="$WORKSPACE/$project"
        [ -d "$repo/.git" ] || continue
        status_log="$RDIR/repos/$project.status.txt"
        branch=$(git -C "$repo" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "?")
        head=$(git -C "$repo" rev-parse --short HEAD 2>/dev/null || echo "?")
        dirty=$(git -C "$repo" status --short 2>/dev/null | wc -l | tr -d ' ')
        {
            git -C "$repo" status --short --branch 2>/dev/null || true
            echo
            git -C "$repo" log -1 --oneline 2>/dev/null || true
        } > "$status_log"
        printf '%s\t%s\t%s\t%s\t%s\n' \
            "$project" "$branch" "$head" "$dirty" "$(rel_path "$status_log")" >> "$RDIR/repo-state.tsv"
    done
}

run_logged() {
    local gate=$1 subject=$2 default_class=$3 kind=$4 workdir=$5 cmd=$6 notes=${7:-}
    local safe log_path rc mapped status step_to
    safe=$(safe_name "$subject")
    log_path="$RDIR/$gate/$safe.log"
    mkdir -p "$(dirname "$log_path")"
    # Hard wall-clock cap for this step. RUN_STEP_TIMEOUT (seconds) is set by the
    # caller gate (e.g. gate_host) so a single wedged step can never stall the
    # whole gate again; 0 (the default) keeps the historic unbounded behaviour.
    step_to="${RUN_STEP_TIMEOUT:-0}"
    [ "$step_to" -gt 0 ] 2>/dev/null || step_to=0
    {
        printf 'cwd=%s\n' "$workdir"
        printf 'cmd=%s\n' "$cmd"
        printf 'started_utc=%s\n' "$(now_utc)"
        [ "$step_to" -gt 0 ] && printf 'step_timeout_s=%s\n' "$step_to"
        echo
    } > "$log_path"
    log "$gate/$subject"
    (
        cd "$workdir" || exit 2
        # Run host steps headless: offscreen Qt platform (as before) so Qt
        # widget/print tests need no real display. NOTE: host-printer coupling
        # (a QPrinter(HighResolution) that probes the host's default printer and
        # blocks — the qterminator test_print_terminal hang) is NOT solved here:
        # Qt ignores CUPS_SERVER for that path. Those tests are excluded from the
        # host gate and belong in a VM lane (no printer => no hang). The
        # per-step timeout below is the universal backstop for any other wedge.
        export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"
        if [ "$step_to" -gt 0 ]; then
            timeout -k 15 "$step_to" bash -lc "$cmd"
        else
            bash -lc "$cmd"
        fi
    ) >> "$log_path" 2>&1
    rc=$?
    if [ "$rc" -eq 124 ]; then
        notes="${notes:+$notes; }step exceeded step_timeout_s=${step_to} and was killed"
    fi
    if [ "$rc" -eq 0 ] && awk '
        body && /^(FAIL|FAIL_LOUD):/ { found=1 }
        /^$/ { body=1 }
        END { exit found ? 0 : 1 }
    ' "$log_path"; then
        rc=$default_class
        notes="${notes:+$notes; }printed FAIL with rc=0"
    fi
    mapped=$(map_rc "$rc" "$default_class")
    if [ "$mapped" -eq 0 ]; then
        status=pass
    else
        status=fail
    fi
    {
        echo
        printf 'finished_utc=%s\n' "$(now_utc)"
        printf 'raw_exit_code=%s\n' "$rc"
        printf 'qci_exit_code=%s\n' "$mapped"
    } >> "$log_path"
    record_result "$gate" "$subject" "$status" "$mapped" "$(exit_class_name "$mapped")" "$kind" "$log_path" "$notes"
    return "$mapped"
}

record_skip() {
    local gate=$1 subject=$2 kind=$3 notes=$4
    record_result "$gate" "$subject" skip 0 pass "$kind" "" "$notes"
}

record_blocked() {
    local gate=$1 subject=$2 class=$3 kind=$4 notes=$5 log_path=${6:-}
    record_result "$gate" "$subject" blocked "$class" "$(exit_class_name "$class")" "$kind" "$log_path" "$notes"
}
# Append one per-task timing row. Single-line O_APPEND is atomic, so this is
# safe to call from concurrent background workers. Columns:
#   gate  subject  provision_s  work_s  total_s  rc  vm
# provision_s = VM acquire/boot time; work_s = test/agent run; total_s = sum.
record_timing() {
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$1" "$2" "$3" "$4" "$5" "$6" "$7" >> "$RDIR/timings.tsv"
}
