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
    # Additive observability siblings (Phase 2 of the test-infra hardening
    # roadmap). These are OBSERVABILITY ONLY — they gate nothing — so they are
    # written as separate files rather than extending the fixed-column
    # results.tsv/timings.tsv contracts that report.py and external readers
    # depend on. scenario-attempts.tsv carries one row per agent attempt (so a
    # future retry shows as attempt 2); host-load.tsv samples the host contention
    # that drives the GUI flake variance.
    printf 'gate\tsubject\tattempt\tstatus\tagent_rc\tclassifier\twall_s\tvm\tlog\tstart_epoch\tend_epoch\tlane\n' > "$RDIR/scenario-attempts.tsv"
    printf 'gate\tsubject\tphase\tloadavg1\tmem_avail_mb\tqemu_vms\tepoch\n' > "$RDIR/host-load.tsv"
    # flake.tsv is the retry LEDGER (Phase 6): one row per failure that carried a
    # retriable infra signature — either `would-retry` (report-only, the default)
    # or the outcome of an actual classified retry. It exists so a retry can NEVER
    # silently collapse a flake into a green: a pass that was reached via retry is
    # always accompanied by a flake.tsv row + a note on the results.tsv row.
    printf 'subject\tclassifier\tfirst_status\tfirst_rc\tretry_status\tattempts\taction\tlog\n' > "$RDIR/flake.tsv"
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
    # Fold any per-worker result fragments back into the canonical TSVs BEFORE the
    # release-escalation scan and report.py read them. Idempotent + a no-op unless
    # a parallel gate wrote fragments; runs here too on an interrupted run (abort_run
    # routes through finish_run) so a killed pool's completed rows are not lost.
    merge_worker_fragments
    # Runner-integrity escalations: (a) quarantined malformed fragment rows must
    # fail the RUN, not just record a fail row (codex review finding — the row
    # alone leaves rc=0 because pool_rc_selfcheck excludes runner-integrity rows);
    # (b) the merged rows must not out-severity the pool-accumulated rc — a
    # pool-gate `fail` row with a rc=0 finish means the `wait -n` plumbing dropped
    # a worker's nonzero exit; fail the run loudly.
    rc=$(fragment_integrity_rc "$rc")
    rc=$(pool_rc_selfcheck "$rc")
    # Reap any write-ahead orphan a worker left behind (spinner created a domain
    # whose name we could not parse, or a worker killed mid-provision before it
    # recorded the parsed name). Baseline-diff scoped, so pre-existing/concurrent
    # VMs are never touched. Runs before golden cleanup so a leaked overlay cannot
    # dangle a backing.
    reap_writeahead_orphans
    # Reclaim per-run golden backing disks (kept if a failed worker was preserved).
    cleanup_run_goldens
    # Release-profile escalation (QCI_RELEASE=1): a `blocked` or `skip` row in a
    # release-relevant gate means the release was not actually exercised — fail
    # closed. Only escalates a run that otherwise PASSED (rc==0); a real failure
    # already carries its own exit class and takes precedence.
    if [ "${QCI_RELEASE:-0}" = 1 ] && [ -f "$RDIR/results.tsv" ]; then
        local incomplete_rows incomplete_count
        incomplete_rows=$(release_profile_incomplete_rows "$RDIR/results.tsv")
        if [ -n "$incomplete_rows" ]; then
            incomplete_count=$(printf '%s\n' "$incomplete_rows" | grep -c .)
            record_result release-profile incomplete-rows fail "$EXIT_RELEASE" release release "" \
                "QCI_RELEASE=1: $incomplete_count skip/blocked row(s) in release-relevant gates are fatal: $(printf '%s' "$incomplete_rows" | tr '\n' ' ')"
            log "release-profile: $incomplete_count skip/blocked row(s) fatal under QCI_RELEASE=1"
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

# Print `<status>:<gate>/<subject>` for release-relevant rows that did not run
# to a pass/fail verdict. Kept separate from finish_run for host-only contract
# tests and report tooling.
release_profile_incomplete_rows() {
    local results=$1
    awk -F'\t' -v g="$QCI_RELEASE_FATAL_GATES" '
        BEGIN { n = split(g, a, " "); for (i = 1; i <= n; i++) fatal[a[i]] = 1 }
        NR > 1 && ($3 == "blocked" || $3 == "skip") && ($1 in fatal) {
            print $3 ":" $1 "/" $2
        }
    ' "$results"
}

abort_run() {
    local sig=$1
    [ "$FINALIZING" = 1 ] && exit "$EXIT_RUNNER"
    log "received $sig; preserving created VMs and finalizing run"
    # Stop and reap any still-running background worker subshells FIRST, so
    # finish_run's merge_worker_fragments can't run while a worker is still
    # appending to its fragment (the no-concurrent-writer invariant). A worker's
    # partial fragment is preserved and merged — that is the crash-diagnosis point.
    local pids=()
    mapfile -t pids < <(jobs -pr)
    if [ "${#pids[@]}" -gt 0 ]; then
        kill "${pids[@]}" 2>/dev/null || true
        wait "${pids[@]}" 2>/dev/null || true
    fi
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

# Where a per-worker record row is appended. Under a PARALLEL worker pool the gate
# sets QCI_RESULT_FRAGMENTS=1 and each worker exports a unique QCI_WORKER_ID, so
# rows land in a per-worker fragment `$RDIR/<name>.d/<worker>.tsv` instead of the
# shared `$RDIR/<name>.tsv`; the parent merges fragments after the pool joins
# (merge_worker_fragments). This removes the dependence on append atomicity under
# concurrency and preserves a crashed worker's rows for post-mortem. A SERIAL run
# (no worker id) writes the canonical file exactly as before — the default path is
# byte-identical, so nothing changes unless a gate opts a parallel pool in.
# Args: name (the TSV basename without .tsv). Pure (env + args) => host-testable.
# A collision-proof, length-bounded worker id: a readable slug (truncated) plus a
# short stable hash of the FULL subject, so two files/scenarios that share a
# basename (foo/x.bats vs bar/x.bats) never share a fragment (which would
# reintroduce the concurrent append we are removing), and a very long path cannot
# produce an overlong filename. Args: gate subject(relative path).
worker_fragment_id() {
    local gate=$1 subject=$2 slug hash
    slug=$(safe_name "$subject")
    slug=${slug:0:96}
    hash=$(printf '%s' "$subject" | cksum | awk '{print $1}')
    printf '%s-%s-%s' "$gate" "$slug" "$hash"
}

record_dest() {
    local name=$1
    if [ "${QCI_RESULT_FRAGMENTS:-0}" = 1 ] && [ -n "${QCI_WORKER_ID:-}" ]; then
        local d="$RDIR/$name.d"
        # On mkdir failure (disk/permissions), fall back to the canonical file so
        # the row is still recorded (degraded to O_APPEND) rather than lost.
        if mkdir -p "$d" 2>/dev/null; then
            printf '%s/%s/%s.tsv' "$RDIR" "$name.d" "$QCI_WORKER_ID"
            return
        fi
    fi
    printf '%s/%s.tsv' "$RDIR" "$name"
}

# Merge per-worker fragments back into the canonical TSVs. A gate calls this AFTER
# its worker pool has joined. Idempotent: each fragment is appended once then
# renamed `.merged` (kept for post-mortem, never re-merged), so a second gate's
# merge skips an earlier gate's fragments. A no-op when no `.d` fragment dirs
# exist (serial runs). Fragments merge in sorted worker-name order for a
# deterministic canonical file.
merge_worker_fragments() {
    local name d frag expected line nf malformed_total=0
    for name in results timings scenario-attempts host-load flake; do
        d="$RDIR/$name.d"
        [ -d "$d" ] || continue
        # Expected per-row column count = the canonical header's field count
        # (init_run wrote it). Deriving it from the header keeps this correct if a
        # schema column is ever added. 0 (no header) => validation is skipped and
        # rows are appended as-is (graceful degradation, never row loss).
        expected=0
        [ -f "$RDIR/$name.tsv" ] && \
            expected=$(head -n1 "$RDIR/$name.tsv" 2>/dev/null | awk -F'\t' '{print NF}')
        while IFS= read -r frag; do
            if [ -s "$frag" ]; then
                # Validate each row's column count and QUARANTINE malformed rows: a
                # worker killed mid-write can leave a short/truncated row that would
                # otherwise silently misalign report parsing. Reading line-by-line
                # and re-emitting each with its own newline also subsumes the old
                # missing-trailing-newline guard. `|| [ -n "$line" ]` keeps a final
                # unterminated (partial) row so it is quarantined, not dropped.
                while IFS= read -r line || [ -n "$line" ]; do
                    [ -n "$line" ] || continue
                    if [ "$expected" -gt 0 ]; then
                        nf=$(printf '%s' "$line" | awk -F'\t' '{print NF}')
                        if [ "$nf" -ne "$expected" ]; then
                            printf '%s\t%s\n' "$(basename "$frag")" "$line" >> "$d/malformed.log"
                            malformed_total=$((malformed_total + 1))
                            continue
                        fi
                    fi
                    printf '%s\n' "$line" >> "$RDIR/$name.tsv"
                done < "$frag"
            fi
            mv "$frag" "$frag.merged"
        done < <(find "$d" -maxdepth 1 -type f -name '*.tsv' | sort)
    done
    # A nonzero quarantine count means a worker crashed mid-write — surface it as
    # a loud runner-integrity row rather than letting a misaligned row skew the
    # report silently.
    if [ "$malformed_total" -gt 0 ]; then
        log "merge_worker_fragments: quarantined $malformed_total malformed fragment row(s) — see *.d/malformed.log"
        record_result runner-integrity fragment-rows fail "$EXIT_RUNNER" runner integrity "" \
            "$malformed_total malformed worker-fragment row(s) quarantined (a worker crashed mid-write); see results.d/malformed.log"
        # Accumulate into the run-global so finish_run can escalate the PROCESS
        # exit code (fragment_integrity_rc): the fail row alone is not enough —
        # pool_rc_selfcheck deliberately ignores runner-integrity rows, so without
        # this a run with quarantined rows could still exit 0.
        MERGE_MALFORMED_TOTAL=$(( ${MERGE_MALFORMED_TOTAL:-0} + malformed_total ))
    fi
}

# Escalate a clean rc when malformed fragment rows were quarantined this run.
# merge_worker_fragments records a runner-integrity fail row for them, but a
# result row does not change the process exit code by itself, and
# pool_rc_selfcheck deliberately excludes runner-integrity rows (it must not
# re-trigger on its own row) — so without this a run whose only failure is a
# quarantined fragment row would exit 0 with an actionable fail row in
# results.tsv. A nonzero incoming rc keeps its original (more specific) class.
# Args: incoming rc. Echoes the (possibly escalated) rc. Host-testable.
fragment_integrity_rc() {
    local rc=$1
    if [ "$rc" -eq 0 ] && [ "${MERGE_MALFORMED_TOTAL:-0}" -gt 0 ]; then
        log "fragment_integrity_rc: $MERGE_MALFORMED_TOTAL malformed fragment row(s) quarantined but rc=0; escalating to EXIT_RUNNER"
        printf '%s' "$EXIT_RUNNER"
        return 0
    fi
    printf '%s' "$rc"
}

# Pool-rc vs recorded-rows self-check (H9). The parallel bats/gui gates accumulate
# their gate rc from `wait -n` worker statuses — correct bash, but fragile against
# future edits: a lost/misread status could let the gate return 0 while a worker
# recorded a `fail` row. Every worker records a result row, so after the fragments
# are merged assert the finish rc is at least as severe as those rows: a `fail` row
# from a POOL gate (bats/gui) with a rc=0 finish means the pool plumbing dropped a
# worker's nonzero exit. Record a loud runner-integrity FAIL row and escalate rc
# to EXIT_RUNNER (same precedent as H4's integrity row). Scope is the pool gates
# ONLY — serial gates set rc via `return`, not `wait -n`, and legitimately record a
# fail row alongside a non-fatal gate rc (e.g. warn-only lint under strict) — and a
# runner-integrity row is excluded so the check never re-triggers on itself.
# One cheap pass over results.tsv. Args: incoming rc. Echoes the (possibly
# escalated) rc. Factored out of finish_run so it is host-testable with stub rows.
pool_rc_selfcheck() {
    local rc=$1 fail_rows nfail
    [ "$rc" -eq 0 ] || { printf '%s' "$rc"; return; }
    [ -f "$RDIR/results.tsv" ] || { printf '%s' "$rc"; return; }
    fail_rows=$(awk -F'\t' '
        NR > 1 && $3 == "fail" && ($1 == "bats" || $1 == "gui") { print $1 "/" $2 }
    ' "$RDIR/results.tsv")
    if [ -n "$fail_rows" ]; then
        nfail=$(printf '%s\n' "$fail_rows" | grep -c .)
        record_result runner-integrity pool-rc fail "$EXIT_RUNNER" runner integrity "" \
            "pool rc=0 but $nfail pool-gate fail row(s) recorded — the wait -n plumbing lost a worker's nonzero exit: $(printf '%s' "$fail_rows" | tr '\n' ' ')"
        log "pool_rc_selfcheck: pool rc=0 but $nfail fail row(s) present; escalating to EXIT_RUNNER"
        rc=$EXIT_RUNNER
    fi
    printf '%s' "$rc"
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
        "$(rel_path "$log_path")" "$notes" "$category" >> "$(record_dest results)"
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
        "$1" "$2" "$3" "$4" "$5" "$6" "$7" >> "$(record_dest timings)"
}

# Append one per-attempt row for an agent scenario. A single `printf >>` is
# atomic for a line this short, so this is safe from the concurrent GUI worker
# pool (same contract as record_result/record_timing). `status` is the RAW agent
# verdict (PASS/FAIL/ERROR/SKIP/UNKNOWN) — distinct from the qci pass/fail row in
# results.tsv — and `classifier` stays empty until the retry classifier (Phase 6)
# fills it. OBSERVABILITY ONLY: nothing here gates a run.
# start_epoch/end_epoch are absolute Unix seconds bounding the attempt (wall_s is
# their difference); lane is the scheduling lane (e.g. admin|qdwin). Both feed the
# report-only correlated-infra-burst detector (an outage is a temporally
# contiguous run of same-classifier infra failures in one lane), which needs real
# completion times, not just durations. All three are trailing/optional so older
# readers and callers are unaffected.
# Columns: gate subject attempt status agent_rc classifier wall_s vm log \
#          start_epoch end_epoch lane
record_attempt() {
    local gate=$1 subject=$2 attempt=$3 status=$4 agent_rc=$5 classifier=${6:-} wall_s=${7:-} vm=${8:-} log_path=${9:-} start_epoch=${10:-} end_epoch=${11:-} lane=${12:-}
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$gate" "$subject" "$attempt" "$status" "$agent_rc" "$classifier" "$wall_s" "$vm" \
        "$(rel_path "$log_path")" "$start_epoch" "$end_epoch" "$lane" >> "$(record_dest scenario-attempts)"
}

# Per-scenario / per-worker HOST scratch directory, unique under the run tree.
# Every agent scenario and bats worker gets its OWN so a fixed shared path (a
# /tmp/*.log, a singleton port-lock file, a "latest artifact" guess) cannot
# collide once GUI/bats lanes run in parallel. Lives under the run dir so it is
# preserved with the run's artifacts for triage and reclaimed when the run dir is
# cleaned. Exported to workers as QCI_SCENARIO_TMPDIR (see gui.sh / bats.sh); a
# QCI_SCENARIO_SLUG is exported alongside so a scenario on a SHARED session VM can
# also isolate GUEST scratch (e.g. /tmp/qci-$QCI_SCENARIO_SLUG). Pure (reads only
# $RDIR + args) => host-testable. Args: gate slug
scenario_scratch_dir() {
    printf '%s/%s/%s.scratch' "$RDIR" "$1" "$2"
}

# Append one host-load sample — a cheap proxy for the contention that drives the
# GUI flake variance (8-vs-25 fails on the same code is host-load-driven). Sample
# at scenario start and end so a slow/failed attempt can be correlated with the
# load it ran under. loadavg1 = 1-min load; mem_avail_mb = reclaimable memory;
# qemu_vms = concurrent qemu domains (the single-tenant lane should be ~1/worker).
# One `printf >>` = atomic. OBSERVABILITY ONLY.
# Columns: gate subject phase loadavg1 mem_avail_mb qemu_vms epoch
record_host_load() {
    local gate=$1 subject=$2 phase=$3 load avail vms
    load=$(awk '{print $1}' /proc/loadavg 2>/dev/null)
    avail=$(awk '/^MemAvailable:/{printf "%d", $2/1024}' /proc/meminfo 2>/dev/null)
    vms=$(pgrep -fc 'qemu-system-x86_64' 2>/dev/null || true)
    [ -n "$vms" ] || vms=0
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$gate" "$subject" "$phase" "${load:-?}" "${avail:-?}" "$vms" "$(date +%s)" \
        >> "$(record_dest host-load)"
}

# Append one retry-ledger row. One `printf >>` = atomic. The `action` records
# what the classified-retry machine did: `would-retry` (report-only default —
# classified retriable but not actually retried), `retried-pass`, `retried-fail`,
# or `retry-vm-provision-failed`. A `retried-pass` row is the MANDATORY companion
# to a green results.tsv row that was only reached via retry — the flake tax is
# never hidden. OBSERVABILITY/AUDIT: this records the retry decision; the gating
# itself lives in gui.sh.
# Columns: subject classifier first_status first_rc retry_status attempts action log
record_flake() {
    local subject=$1 classifier=$2 first_status=$3 first_rc=$4 \
        retry_status=${5:-} attempts=${6:-1} action=${7:-} log_path=${8:-}
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$subject" "$classifier" "$first_status" "$first_rc" "$retry_status" \
        "$attempts" "$action" "$(rel_path "$log_path")" >> "$(record_dest flake)"
}
