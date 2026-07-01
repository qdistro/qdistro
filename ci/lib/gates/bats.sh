#!/usr/bin/env bash
# qci module: bats gate (disposable VM pool)
# Extracted verbatim from bin/qci. SOURCED by bin/qci into the single
# CI-runner process (shared RDIR/CREATED_VMS/golden state/traps); it is
# NOT executed standalone. See ci/AGENTS.md for the module map.
# shellcheck shell=bash

# Pick how many disposable bats VMs to run concurrently. Each VM is ~4 GiB RAM
# + QDWIN_VM_VCPUS vCPUs, so RAM is the binding constraint (CPU is safe to
# overprovision). Three tiers, auto-selected from host RAM + logical CPUs:
#   minimal  (<=32 GiB / few cores) -> 4
#   medium   (~64 GiB, >=10 cores)  -> 10
#   high     (>=90 GiB, >=12 cores) -> 16
# QCI_JOBS overrides outright. The chosen value is RAM-clamped so VMs can never
# oversubscribe memory regardless of tier.
bats_job_count() {
    local jobs ram_gb avail_gb cores ram_cap
    # Tier selection uses MemTotal (the machine's class); the safety clamp uses
    # MemAvailable (current headroom incl. reclaimable cache) so a busy host
    # can't be overcommitted even though the machine is nominally large.
    ram_gb=$(awk '/^MemTotal:/{printf "%d", $2/1024/1024}' /proc/meminfo 2>/dev/null)
    [ -n "$ram_gb" ] 2>/dev/null || ram_gb=8
    avail_gb=$(awk '/^MemAvailable:/{printf "%d", $2/1024/1024}' /proc/meminfo 2>/dev/null)
    [ -n "$avail_gb" ] && [ "$avail_gb" -gt 0 ] 2>/dev/null || avail_gb=$ram_gb
    if [ -n "${QCI_JOBS:-}" ] && [ "${QCI_JOBS}" -ge 1 ] 2>/dev/null; then
        jobs=$QCI_JOBS
    else
        cores=$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || echo 2)
        if   [ "$ram_gb" -ge 90 ] && [ "$cores" -ge 12 ]; then jobs=16   # high
        elif [ "$ram_gb" -ge 56 ] && [ "$cores" -ge 10 ]; then jobs=10   # medium
        else                                                   jobs=4    # minimal
        fi
    fi
    # RAM safety clamp: ~5 GiB/VM, keep ~6 GiB for the host. Clamps even a
    # QCI_JOBS override so a typo can't oversubscribe memory.
    ram_cap=$(( (avail_gb - 6) / 5 ))
    [ "$ram_cap" -lt 1 ] && ram_cap=1
    [ "$jobs" -gt "$ram_cap" ] && jobs=$ram_cap
    [ "$jobs" -lt 1 ] && jobs=1
    printf '%s\n' "$jobs"
}

# Run one bats file against an already-up VM; collect artifacts and record the
# pass/fail row. Returns 0 on pass, EXIT_BATS on failure. Does NOT acquire or
# release the VM — the caller owns its lifecycle. Safe to run backgrounded:
# every shared write (results.tsv, manifest.txt, the per-file log, the per-VM
# artifact dir) is either a single-line O_APPEND or a path unique to this VM.
bats_run_one() {
    qci_assert_vm_exists "$1" "bats:$(basename "$2")" || return $?
    local vm=$1 file=$2 base log_path gate_rc slug scratch
    base=$(basename "$file")
    log_path="$RDIR/bats/$base.log"
    mkdir -p "$(dirname "$log_path")"
    # Per-file isolated scratch dir (host) + slug (guest suffix), so a bats file
    # routes scratch here instead of a collision-prone fixed path once the pool
    # runs several files in parallel.
    slug=$(safe_name "$base")
    scratch=$(scenario_scratch_dir bats "$slug")
    mkdir -p "$scratch"
    log "bats $base on $vm"
    (
        cd "$QDISTRO_REPO" || exit 2
        VM_NAME="$vm" QCI_OFFLINE="$QCI_OFFLINE" \
            QCI_SCENARIO_TMPDIR="$scratch" QCI_SCENARIO_SLUG="$slug" bats "$file"
    ) > "$log_path" 2>&1
    gate_rc=$?
    collect_vm_artifacts "$vm" "bats-${base%.bats}"
    if [ "$gate_rc" -eq 0 ]; then
        record_result bats "$base" pass 0 pass bats "$log_path" "VM=$vm"
        return 0
    fi
    record_result bats "$base" fail "$EXIT_BATS" bats bats "$log_path" "VM=$vm raw_rc=$gate_rc"
    return "$EXIT_BATS"
}

# Acquire a fresh disposable VM, run one bats file on it, then release it.
# Self-contained so it can run as a backgrounded pool worker. Returns EXIT_BATS
# on test failure, EXIT_VM_PROVISION if the VM could not be created, else 0.
bats_run_disposable() {
    local file=$1 base vm frc t0 t1 t2
    base=$(basename "$file")
    t0=$(date +%s)
    vm=$(acquire_vm "bats-$(safe_name "${base%.bats}")" "") || {
        record_timing bats "$base" "$(( $(date +%s) - t0 ))" 0 "$(( $(date +%s) - t0 ))" provfail ""
        return "$EXIT_VM_PROVISION"
    }
    t1=$(date +%s)
    bats_run_one "$vm" "$file"
    frc=$?
    t2=$(date +%s)
    release_vm "$vm" "$frc"
    record_timing bats "$base" "$((t1 - t0))" "$((t2 - t1))" "$((t2 - t0))" "$frc" "$vm"
    return "$frc"
}

gate_bats() {
    qci_assert_run_dir || return $?
    local explicit=${1:-}; shift || true
    local files=("$@")
    local rc=$EXIT_OK file base frc running jobs
    if [ "${#files[@]}" -eq 0 ]; then
        while IFS= read -r file; do files+=("$file"); done < <(find "$QDISTRO_REPO/tests/integration/vm" -maxdepth 1 -name '*.bats' -type f | sort)
    fi
    if ! command -v bats >/dev/null 2>&1; then
        record_blocked bats bats "$EXIT_PREFLIGHT" bats "bats executable not found"
        return "$EXIT_PREFLIGHT"
    fi

    # Filter out offline-skipped files up front (cheap; no VM, no worker slot).
    # QCI_OFFLINE annotation hook: if the registry marks this test's network
    # column 'external', skip it in offline mode rather than let it try (and
    # fail) to reach the network. Tests not in the registry see QCI_OFFLINE=1 in
    # their env and are expected to self-skip.
    local run_files=()
    for file in "${files[@]}"; do
        base=$(basename "$file")
        if offline_should_skip_external "tests/integration/vm/$base"; then
            record_result bats "$base" skip 0 pass bats "" "QCI_OFFLINE=1: registry network=external; skipped"
            continue
        fi
        run_files+=("$file")
    done
    [ "${#run_files[@]}" -eq 0 ] && return "$rc"

    # Explicit VM: every file shares one caller-provided VM, so they MUST run
    # serially (the in-VM session/sockets are single-tenant).
    if [ -n "$explicit" ]; then
        validate_vm bats "$explicit" || return "$EXIT_VM_PROVISION"
        for file in "${run_files[@]}"; do
            bats_run_one "$explicit" "$file"
            frc=$?
            [ "$frc" -ne 0 ] && [ "$rc" -eq 0 ] && rc=$frc
        done
        return "$rc"
    fi

    # Build the per-run golden ONCE (compositor built from current source), so
    # every worker clones it and skips the in-guest build. Fail the gate fast if
    # the golden can't be built — do not silently fall back to per-worker builds.
    # Opt out with QCI_NO_GOLDEN=1 (workers then each run the full bootstrap).
    if [ "${QCI_NO_GOLDEN:-0}" != 1 ]; then
        ensure_run_golden bats || return "$EXIT_VM_PROVISION"
    fi

    # Disposable VMs: run a bounded pool of one-VM-per-file workers in parallel.
    # Concurrency is capped at `jobs` via `wait -n` (reap one worker before
    # launching the next once at capacity).
    jobs=$(bats_job_count)
    # Concurrency visibility (Phase 0 / H8): record the requested vs effective
    # (RAM-clamped) parallelism in manifest.txt so a reader sees the caps without
    # external monitor notes.
    kv bats_jobs_requested "${QCI_JOBS:-auto}"
    kv bats_jobs_effective "$jobs"
    # Per-worker result fragments (merged at finish_run) when >1 file runs
    # concurrently, so parallel appends never interleave and a crashed worker's
    # rows survive. Operator forces off with QCI_RESULT_FRAGMENTS=0.
    local frag=0 worker_id
    [ "$jobs" -gt 1 ] && frag=${QCI_RESULT_FRAGMENTS:-1}
    log "bats gate: ${#run_files[@]} files on disposable VMs, up to $jobs in parallel (set QCI_JOBS to override)"
    running=0
    for file in "${run_files[@]}"; do
        # Full relative path (not just basename) so two files sharing a basename
        # across dirs never collide onto one fragment.
        worker_id=$(worker_fragment_id bats "${file#"$WORKSPACE"/}")
        QCI_RESULT_FRAGMENTS="$frag" QCI_WORKER_ID="$worker_id" \
            bats_run_disposable "$file" &
        running=$((running + 1))
        if [ "$running" -ge "$jobs" ]; then
            wait -n; frc=$?
            [ "$frc" -ne 0 ] && [ "$rc" -eq 0 ] && rc=$frc
            running=$((running - 1))
        fi
    done
    while [ "$running" -gt 0 ]; do
        wait -n; frc=$?
        [ "$frc" -ne 0 ] && [ "$rc" -eq 0 ] && rc=$frc
        running=$((running - 1))
    done
    return "$rc"
}
