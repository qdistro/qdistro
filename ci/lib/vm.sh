#!/usr/bin/env bash
# qci module: VM lifecycle, per-run golden, artifacts
# Extracted verbatim from bin/qci. SOURCED by bin/qci into the single
# CI-runner process (shared RDIR/CREATED_VMS/golden state/traps); it is
# NOT executed standalone. See ci/AGENTS.md for the module map.
# shellcheck shell=bash

is_protected_vm() {
    case "$1" in
        qdistro-daily|qdistro-daily-*) return 0 ;;
        *) return 1 ;;
    esac
}

validate_vm() {
    local gate=$1 vm=$2
    if [ -z "$vm" ]; then
        log "empty VM name"
        return "$EXIT_VM_PROVISION"
    fi
    if is_protected_vm "$vm" && [ "${QCI_FORCE_PROTECTED_VM:-0}" != 1 ]; then
        log "refusing protected VM '$vm' for $gate; set QCI_FORCE_PROTECTED_VM=1 to override"
        return "$EXIT_VM_PROVISION"
    fi
    if ! "${VIRSH[@]}" dominfo "$vm" >/dev/null 2>&1; then
        log "VM '$vm' not found"
        return "$EXIT_VM_PROVISION"
    fi
    return 0
}

# List qci domains (defined + running) whose name starts with $prefix, one per
# line. Used by the write-ahead orphan-reaper. Pure w.r.t. libvirt read-only.
vm_list_by_prefix() {
    local prefix=$1 d
    "${VIRSH[@]}" list --all --name 2>/dev/null | while IFS= read -r d; do
        [ -n "$d" ] || continue
        case "$d" in "$prefix"*) printf '%s\n' "$d" ;; esac
    done
}

# Parse the YYMMDD-HHMMSS timestamp that clone-baseweed.sh embeds in a spinner-
# created domain name (`<prefix>YYMMDD-HHMMSS-<pid>-<rand>`) and echo it as epoch
# seconds. Returns nonzero WITH NO OUTPUT if the remainder after `prefix` does not
# begin with the expected `NNNNNN-NNNNNN-` pattern (the caller must then fail
# safe and NOT reap the domain). Pure: only reads its args + the local `date`.
vm_name_epoch() {
    local dom=$1 prefix=$2 rest ymd hms epoch
    rest=${dom#"$prefix"}
    case "$rest" in
        [0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]-*) ;;
        *) return 1 ;;
    esac
    ymd=${rest:0:6}; hms=${rest:7:6}
    epoch=$(date -d "20${ymd:0:2}-${ymd:2:2}-${ymd:4:2} ${hms:0:2}:${hms:2:2}:${hms:4:2}" +%s 2>/dev/null) \
        || return 1
    [ -n "$epoch" ] || return 1
    printf '%s\n' "$epoch"
}

# Reap qci disposable domains that (a) match a gate's unique write-ahead prefix,
# (b) did NOT exist at the baseline captured just before this run's spinner ran
# (so a pre-existing or concurrent-run VM is never touched — VM names are not
# run-unique, only gate-prefixed, so the baseline diff is the shared-host safety
# guard), (c) are not already tracked in created-vms.txt, and (d) carry an
# embedded YYMMDD-HHMMSS timestamp that falls inside THIS acquire's provisioning
# window [win_start-margin, win_end+margin] (FINDING 2: the baseline diff alone
# does not protect a concurrent same-gate-prefix run that created its domain
# AFTER our baseline snapshot; the window discriminates it). A name that does not
# parse as the spinner pattern is NEVER reaped (fail safe, logged). This is the
# backstop for H2 (spinner created a domain but its name could not be parsed) and
# a worker killed (kill -9) mid-provision before it could record the parsed name.
# Never touches protected VMs or golden disks. Best-effort; never fatal.
# Args: prefix baseline_csv(comma-separated names present before the spinner) \
#       win_start(epoch) win_end(epoch)
# When win_start/win_end are both empty the window/parse gate is skipped (only
# baseline + tracked filtering applies); acquire_vm and the .wa marker sweep
# always supply the window, so real reaps are always window-scoped.
reap_new_orphans() {
    local prefix=$1 baseline_csv=${2:-} win_start=${3:-} win_end=${4:-}
    local dom ddisk tracked="" d_epoch margin
    margin=${QCI_REAP_WINDOW_MARGIN_S:-120}
    [ "$margin" -ge 0 ] 2>/dev/null || margin=120
    [ -n "$prefix" ] || return 0
    [ -f "$RDIR/vm/created-vms.txt" ] && tracked=$(cat "$RDIR/vm/created-vms.txt" 2>/dev/null)
    while IFS= read -r dom; do
        [ -n "$dom" ] || continue
        # Skip anything that existed at baseline (pre-existing / concurrent run).
        case ",$baseline_csv," in *",$dom,"*) continue ;; esac
        is_protected_vm "$dom" && continue
        printf '%s\n' "$tracked" | grep -Fxq "$dom" && continue
        # Concurrent-run guard: only reap inside this acquire's timestamp window.
        if [ -n "$win_start" ] && [ -n "$win_end" ]; then
            if ! d_epoch=$(vm_name_epoch "$dom" "$prefix"); then
                log "reap_new_orphans: NOT reaping $dom — name does not parse as spinner pattern (fail safe)"
                continue
            fi
            if [ "$d_epoch" -lt "$((win_start - margin))" ] || [ "$d_epoch" -gt "$((win_end + margin))" ]; then
                log "reap_new_orphans: NOT reaping $dom — embedded ts $d_epoch outside window [$win_start,$win_end] +/-${margin}s (concurrent run?)"
                continue
            fi
        fi
        log "reap_new_orphans: reaping untracked orphan $dom (write-ahead prefix $prefix)"
        ddisk=$(vm_disk_path "$dom" 2>/dev/null || true)
        "${VIRSH[@]}" destroy "$dom" >/dev/null 2>&1 || true
        "${VIRSH[@]}" undefine "$dom" --nvram --managed-save >/dev/null 2>&1 \
            || "${VIRSH[@]}" undefine "$dom" >/dev/null 2>&1 || true
        [ -n "$ddisk" ] && [ -f "$ddisk" ] && safe_rm_overlay "$ddisk" >/dev/null 2>&1 || true
    done < <(vm_list_by_prefix "$prefix")
}

# End-of-run / interrupt sweep: process any LEFTOVER write-ahead markers (a worker
# that died mid-provision never removed its own). Each marker records the gate
# prefix, the pre-spinner baseline, and the provisioning window start (win_start),
# so reap_new_orphans reaps only this run's untracked creations that fall inside
# [win_start, now] — a domain a concurrent same-prefix run created before this
# dead worker's win_start is never touched. Idempotent: markers are removed as
# they are processed.
reap_writeahead_orphans() {
    local wa_dir="$RDIR/vm/provisioning.d" waf prefix baseline win_start now
    [ -d "$wa_dir" ] || return 0
    now=$(date +%s)
    for waf in "$wa_dir"/*.wa; do
        [ -e "$waf" ] || continue
        prefix=$(awk -F'\t' '$1=="prefix"{print $2; exit}' "$waf" 2>/dev/null)
        baseline=$(awk -F'\t' '$1=="baseline"{print $2; exit}' "$waf" 2>/dev/null)
        win_start=$(awk -F'\t' '$1=="win_start"{print $2; exit}' "$waf" 2>/dev/null)
        # A dead worker never recorded win_end; the sweep runs after any domain it
        # created, so `now` is a valid upper bound. If the marker predates the
        # window format (no win_start) fall back to baseline-only filtering.
        [ -n "$prefix" ] && reap_new_orphans "$prefix" "$baseline" "$win_start" "${win_start:+$now}"
        rm -f "$waf" 2>/dev/null || true
    done
}

acquire_vm() {
    local gate=$1 explicit=${2:-} log_path vm spinner gui_session=""
    if [ -n "$explicit" ]; then
        validate_vm "$gate" "$explicit" || return "$EXIT_VM_PROVISION"
        printf '%s\n' "$explicit"
        return 0
    fi
    log_path="$RDIR/vm/spin-$gate.log"
    log "creating disposable VM for $gate"
    spinner=spin-test-vm.sh
    # Match the per-scenario workers too: gui_run_scenario acquires VMs as
    # `gui-<scenario>`, not bare `gui`. An exact `= gui` test here left every
    # per-scenario GUI worker on the broker-only spin-test-vm.sh — no labwc/lxqt,
    # no work/work2 users, no admin app / TUI / approvals CLI — so the GUI agent
    # scenarios all ERRORed on "broker-only VM, missing GUI components". Use the
    # same gui|gui-* glob as the golden_backing case just below.
    case "$gate" in
        gui-qdwin|gui-qdwin-*) spinner=spin-test-vm-gui.sh; gui_session=qdwin ;;
        gui|gui-admin|gui-admin-*|gui-*) spinner=spin-test-vm-gui.sh; gui_session=labwc ;;
    esac
    # If a per-run golden has been built for this gate's family, clone from it
    # (the spinner then skips fresh-vm-bootstrap). Empty => normal full build.
    local golden_backing=""
    case "$gate" in
        bats|bats-*) golden_backing="$RUN_GOLDEN_BATS" ;;
        gui-qdwin|gui-qdwin-*) golden_backing="$RUN_GOLDEN_GUI_QDWIN" ;;
        gui|gui-admin|gui-admin-*|gui-*) golden_backing="$RUN_GOLDEN_GUI_ADMIN" ;;
    esac
    # Bounded wait (H1): a wedged spinner or a slow/stuck disk must not stall the
    # whole run silently. `timeout` caps the spinner; a breach is classified as
    # vm_provision infra (feeds the correlated-burst detector) with a clear note.
    # Generous default (a from-golden clone is fast, but a no-golden full build is
    # heavy); override with QCI_VM_PROVISION_TIMEOUT_S.
    local prov_timeout=${QCI_VM_PROVISION_TIMEOUT_S:-1800} rc
    local t_start t_end
    # Write-ahead (H2/H3): snapshot the domains that already carry this gate's
    # unique prefix BEFORE the spinner, and drop a marker recording the prefix +
    # baseline. If the spinner creates a domain but we cannot parse its name, or a
    # worker is killed mid-provision before recording it, the domain is untracked;
    # the baseline-diff reaper (reap_new_orphans / reap_writeahead_orphans) reaps
    # only THIS run's untracked creations, never a pre-existing/concurrent-run VM.
    local wa_prefix="qci-$gate-" wa_baseline wa_file=""
    wa_baseline=$(vm_list_by_prefix "$wa_prefix" | tr '\n' ',')
    # Capture win_start BEFORE the spinner: the domain's embedded YYMMDD-HHMMSS
    # timestamp is minted by clone-baseweed.sh once the spinner runs, so it is
    # always >= win_start. The reaper uses [win_start, t_end] to reject a
    # concurrent same-prefix run's domain (FINDING 2).
    t_start=$(date +%s)
    if mkdir -p "$RDIR/vm/provisioning.d" 2>/dev/null; then
        wa_file="$RDIR/vm/provisioning.d/${QCI_WORKER_ID:-main}-$$.wa"
        { printf 'prefix\t%s\n' "$wa_prefix"; printf 'baseline\t%s\n' "$wa_baseline"
          printf 'win_start\t%s\n' "$t_start"; } \
            > "$wa_file" 2>/dev/null || wa_file=""
    fi
    timeout "$prov_timeout" env \
        QDWIN_VM_TEMPLATE="${QDWIN_VM_TEMPLATE:-qdistro-template}" \
        QCI_RUN_GOLDEN_BACKING="$golden_backing" \
        QDWIN_APP_DEPS="${QDWIN_APP_DEPS:-0}" \
        QDISTRO_VM_GUI_SESSION="${gui_session:-${QDISTRO_VM_GUI_SESSION:-}}" \
        "$VM_TOOLS/$spinner" "qci-$gate" > "$log_path" 2>&1
    rc=$?
    t_end=$(date +%s)
    if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
        log "acquire_vm: $spinner for $gate exceeded ${prov_timeout}s (bounded wait) — classifying vm_provision infra"
        # A killed spinner may have left a half-created domain; reap this call's
        # new orphans before the write-ahead marker is cleared.
        reap_new_orphans "$wa_prefix" "$wa_baseline" "$t_start" "$t_end"
        [ -n "$wa_file" ] && rm -f "$wa_file" 2>/dev/null || true
        record_attempt "$gate" "$spinner" 1 TIMEOUT "$rc" vm-provision "$((t_end - t_start))" "" "$log_path" "$t_start" "$t_end" "$gate"
        record_result "$gate" "$spinner" fail "$EXIT_VM_PROVISION" vm_provision vm "$log_path" "VM provisioning exceeded ${prov_timeout}s (bounded wait; likely wedged spinner or slow disk). Override with QCI_VM_PROVISION_TIMEOUT_S."
        return "$EXIT_VM_PROVISION"
    fi
    if [ "$rc" -ne 0 ]; then
        # The spinner's own SPUN_OK teardown handles the clean-failure path, but
        # reap any new orphan it left as a backstop.
        reap_new_orphans "$wa_prefix" "$wa_baseline" "$t_start" "$t_end"
        [ -n "$wa_file" ] && rm -f "$wa_file" 2>/dev/null || true
        record_result "$gate" "$spinner" fail "$EXIT_VM_PROVISION" vm_provision vm "$log_path" "VM creation failed"
        return "$EXIT_VM_PROVISION"
    fi
    vm=$(grep -E "^qci-[A-Za-z0-9._-]+$" "$log_path" | tail -n 1 | tr -d '[:space:]')
    [ -n "$vm" ] || vm=$(tail -n 1 "$log_path" | tr -d '[:space:]')
    if [ -z "$vm" ]; then
        # H2 zombie: the spinner succeeded (rc 0) but its output carried no
        # parseable VM name, so a running domain it created is untracked and
        # end-of-run cleanup would miss it. Reap this call's new orphans
        # (baseline-diff scoped) before returning.
        reap_new_orphans "$wa_prefix" "$wa_baseline" "$t_start" "$t_end"
        [ -n "$wa_file" ] && rm -f "$wa_file" 2>/dev/null || true
        record_result "$gate" "$spinner" fail "$EXIT_VM_PROVISION" vm_provision vm "$log_path" "$spinner produced no VM name (untracked domains reaped by baseline diff)"
        return "$EXIT_VM_PROVISION"
    fi
    CREATED_VMS+=("$vm")
    printf '%s\n' "$vm" >> "$RDIR/vm/created-vms.txt"
    # Exact name now tracked in created-vms.txt; the write-ahead marker is no
    # longer needed (its whole purpose was to cover the pre-record window).
    [ -n "$wa_file" ] && rm -f "$wa_file" 2>/dev/null || true
    kv "vm_${gate}" "$vm"
    printf '%s\n' "$vm"
}

release_vm() {
    local vm=$1 rc=$2
    [ -n "$vm" ] || return 0
    if is_protected_vm "$vm"; then
        log "preserving protected VM $vm"
        return 0
    fi
    local created=0 v
    for v in "${CREATED_VMS[@]:-}"; do
        [ "$v" = "$vm" ] && created=1
    done
    if [ "$created" = 0 ] && [ -f "$RDIR/vm/created-vms.txt" ]; then
        grep -Fxq "$vm" "$RDIR/vm/created-vms.txt" && created=1
    fi
    [ "$created" = 1 ] || {
        log "preserving pre-existing VM $vm"
        return 0
    }
    if [ "$rc" -ne 0 ] && [ "${QCI_DELETE_FAILED_VM:-0}" != 1 ] && [ "${QCI_KEEP_FAILED_VM:-$KEEP_FAILED_DEFAULT}" = 1 ]; then
        # Preserve the failed VM for debugging, but do NOT leave it running.
        # A long multi-VM gate (bats spins one disposable VM per file) would
        # otherwise pile up a 4 GB running guest per failure and exhaust host
        # RAM. First try to hibernate via `managedsave` (saves live guest state
        # to a file and stops the domain, so `virsh start <vm>` resumes the
        # exact failed state). NOTE: the qdistro-template CPU is
        # host-passthrough with migratable='off' (the invtsc flag is
        # non-migratable), and managedsave uses the migration path — so for
        # these VMs managedsave fails and we fall back to a plain power-off
        # (destroy). Either way the definition + overlay disk are preserved and
        # `virsh start <vm>` brings the VM back for triage (a powered-off VM
        # boots fresh from disk; on-disk logs/journals are intact). RAM/CPU are
        # freed in both cases. Artifacts were already pulled by the caller's
        # collect_vm_artifacts before release_vm runs.
        # Escape hatch: QCI_KEEP_FAILED_VM_RUNNING=1 leaves it running as before.
        log "preserving failed VM $vm"
        printf 'preserved_failed_vm=%s\n' "$vm" >> "$RDIR/manifest.txt"
        # This overlay's backing chain may point at a per-run golden; keep the
        # golden disk(s) so the preserved VM stays bootable for triage. Record
        # this via a PERSISTENT MARKER FILE, not just the GOLDEN_PRESERVE shell
        # var: in the parallel bats/gui pools release_vm runs inside a
        # backgrounded `&` worker subshell, so a var assigned here never reaches
        # the parent that later runs cleanup_run_goldens — the golden would be
        # deleted out from under the preserved overlay and `virsh start <vm>`
        # for triage would fail on a dangling backing. The marker file survives
        # the subshell; GOLDEN_PRESERVE is kept too for parent-side callers
        # (abort_run).
        if [ "${#RUN_GOLDEN_DISKS[@]}" -gt 0 ]; then
            GOLDEN_PRESERVE=1
            # H12: the parent (cleanup_run_goldens) learns of the preserve ONLY
            # through this marker file — the GOLDEN_PRESERVE var above cannot cross
            # the backgrounded worker subshell. If the marker write fails (disk
            # full, unwritable dir), the parent could delete a golden this
            # preserved overlay still backs, stranding the triage disk chain.
            # VERIFY the write and, on any failure, log LOUDLY. cleanup_run_goldens
            # additionally double-checks backing referrers, so a missing marker
            # still takes the SAFE (preserve) path there.
            if ! { mkdir -p "$RDIR/vm" && : > "$RDIR/vm/golden-preserve"; } 2>/dev/null \
                 || [ ! -e "$RDIR/vm/golden-preserve" ]; then
                log "WARNING: FAILED to write golden-preserve marker ($RDIR/vm/golden-preserve) for preserved VM $vm — golden deletion now relies on the backing-referrer safety check in cleanup_run_goldens"
                printf 'golden_preserve_marker_write_failed=%s\n' "$vm" >> "$RDIR/manifest.txt" 2>/dev/null || true
            fi
        fi
        local preserved_state notes
        if [ "${QCI_KEEP_FAILED_VM_RUNNING:-0}" = 1 ]; then
            log "  QCI_KEEP_FAILED_VM_RUNNING=1 — leaving $vm running"
            preserved_state=running
            notes="failed VM preserved (left running) for debugging"
        elif "${VIRSH[@]}" managedsave "$vm" >/dev/null 2>&1; then
            log "  hibernated $vm (managedsave) — resume with: virsh -c qemu:///session start $vm"
            preserved_state=hibernated
            notes="failed VM hibernated (managedsave) for debugging; virsh start to resume"
        else
            "${VIRSH[@]}" destroy "$vm" >/dev/null 2>&1 || true
            log "  managedsave failed; powered off $vm (disk preserved) — restart with: virsh -c qemu:///session start $vm"
            preserved_state=powered_off
            notes="failed VM powered off (disk preserved) for debugging; virsh start to inspect"
        fi
        printf 'preserved_failed_vm_state=%s\n' "$preserved_state" >> "$RDIR/manifest.txt"
        record_result lifecycle "$vm" skip 0 pass vm "" "$notes"
        return 0
    fi
    log "destroying VM $vm"
    local disk
    disk=$(vm_disk_path "$vm" || true)
    "${VIRSH[@]}" destroy "$vm" >/dev/null 2>&1 || true
    # --managed-save: a previously-hibernated failed VM has a managedsave image;
    # undefine refuses to remove it otherwise.
    "${VIRSH[@]}" undefine "$vm" --remove-all-storage --managed-save >/dev/null 2>&1 || {
        "${VIRSH[@]}" undefine "$vm" --nvram --managed-save >/dev/null 2>&1 || true
    }
    # On qemu:///session, `undefine --remove-all-storage` returns 0 but does NOT
    # delete the overlay qcow2 (it is not a pool-tracked managed volume), so the
    # `||` fallback never fired and the overlay leaked. Unconditionally reclaim
    # the overlay if it survived undefine. safe_rm_overlay guards path/name so
    # this is a harmless no-op when --remove-all-storage actually deleted it.
    if [ -n "$disk" ] && [ -f "$disk" ]; then
        if safe_rm_overlay "$disk"; then
            log "reclaimed leaked overlay $disk"
        fi
    fi
}

# --- qci storage-namespace lock (ROUND 5, destructive defect 4) -------------
#
# ROUND 3 put an exclusive flock on `$QDWIN_IMG_DIR/.qci-storage.lock` around
# the TWO deletion sites in the cleanup gate. Round 4's review found that this
# left the protocol incomplete: `cleanup_run_goldens` (and every other route
# that reaches safe_rm_overlay -- release_vm's leaked-overlay reclaim,
# reap_new_orphans, the golden-build failure paths, abort_run's in-flight
# golden reap) unlinked images with no lock at all, while
# scripts/vm/clone-baseweed.sh was inside its create..define window believing
# the protocol covered it.
#
# The lock therefore lives HERE, next to the unlink, and safe_rm_overlay takes
# it itself. Every present and future caller is inside the protocol by
# construction rather than by a reviewer noticing the call site.
#
# Re-entrant by depth count, because the cleanup gate must hold the SAME lock
# across a wider critical section (final ownership refresh -> final backing
# referrer audit -> unlink) than the unlink alone. flock(2) is per open file
# DESCRIPTION, so a second open+flock from the same process would deadlock
# against the first; the depth counter makes the inner acquisition a no-op.
# Subshells (`$(...)`, backgrounded workers) inherit the open descriptor and
# therefore genuinely hold the lock the copied depth claims.
#
# Failing to take the lock is NEVER "unlink anyway": it returns failure and
# the image survives. A leaked overlay is recoverable; a deleted live backing
# is not.
QCI_STORAGE_LOCK_FD=""
QCI_STORAGE_LOCK_DEPTH=0
QCI_STORAGE_LOCK_REASON=""

qci_storage_lock_path() {
    printf '%s/.qci-storage.lock' \
        "${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}"
}

qci_storage_lock_acquire() {
    local wait=${1:-${QCI_CLEANUP_LOCK_WAIT:-120}} lock
    QCI_STORAGE_LOCK_REASON=""
    if [ "${QCI_STORAGE_LOCK_DEPTH:-0}" -gt 0 ]; then
        QCI_STORAGE_LOCK_DEPTH=$((QCI_STORAGE_LOCK_DEPTH + 1))
        return 0
    fi
    if ! command -v flock >/dev/null 2>&1; then
        QCI_STORAGE_LOCK_REASON="storage lock unavailable: no flock(1)"
        return 1
    fi
    lock=$(qci_storage_lock_path)
    QCI_STORAGE_LOCK_FD=""
    # The braces matter: a bare `exec REDIR 2>/dev/null` has no command, so
    # BOTH redirections become permanent and the shell loses stderr for the
    # rest of its life. Scope the error suppression to the group.
    if ! { exec {QCI_STORAGE_LOCK_FD}>>"$lock"; } 2>/dev/null; then
        QCI_STORAGE_LOCK_FD=""
        QCI_STORAGE_LOCK_REASON="storage lock cannot be opened: $lock"
        return 1
    fi
    if ! flock -w "$wait" -x "$QCI_STORAGE_LOCK_FD"; then
        eval "exec ${QCI_STORAGE_LOCK_FD}>&-" 2>/dev/null || true
        QCI_STORAGE_LOCK_FD=""
        QCI_STORAGE_LOCK_REASON="storage lock not acquired within ${wait}s; a worker holds it"
        return 1
    fi
    QCI_STORAGE_LOCK_DEPTH=1
    return 0
}

qci_storage_lock_release() {
    [ "${QCI_STORAGE_LOCK_DEPTH:-0}" -gt 0 ] || return 0
    QCI_STORAGE_LOCK_DEPTH=$((QCI_STORAGE_LOCK_DEPTH - 1))
    [ "$QCI_STORAGE_LOCK_DEPTH" -eq 0 ] || return 0
    [ -n "$QCI_STORAGE_LOCK_FD" ] || return 0
    eval "exec ${QCI_STORAGE_LOCK_FD}>&-" 2>/dev/null || true
    QCI_STORAGE_LOCK_FD=""
    return 0
}

# Guarded removal of a qci disposable overlay. Only removes a path that is a
# regular non-empty file, lives directly under the libvirt images dir, and
# whose basename starts with `qci-` (so backing images like baseweed-baked.qcow2
# or qdistro-daily* are never touched). Returns 0 only if it removed the file.
safe_rm_overlay() {
    local path=$1
    [ -n "$path" ] || return 1
    local img_dir base rm_rc=0
    img_dir="${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}"
    base=$(basename -- "$path")
    case "$base" in
        qci-*) ;;
        *) return 1 ;;
    esac
    # Must reside directly in the images dir (not a symlink/escape). Pure
    # string tests, so they run before the lock is taken.
    [ "$path" = "$img_dir/$base" ] || return 1
    # ROUND 5: the storage lock is taken HERE, so that every qci deletion
    # route -- not just the cleanup gate's two sites -- is serialised against
    # scripts/vm/clone-baseweed.sh's `qemu-img create -b` .. `virsh define`
    # window. Cleanup already holds it across its wider critical section; the
    # depth counter makes this acquisition a no-op there.
    if ! qci_storage_lock_acquire; then
        log "refusing to unlink $path: ${QCI_STORAGE_LOCK_REASON:-storage lock unavailable}"
        return 1
    fi
    # Every filesystem predicate is re-evaluated INSIDE the lock: the caller's
    # earlier `[ -f ]` proved nothing about the state at unlink time.
    if [ ! -f "$path" ]; then
        rm_rc=1
    # ROUND 3: a SYMLINK named qci-*.qcow2 passes every other guard, but
    # unlinking it removes the link while every audit that authorised the
    # removal inspected its TARGET. Audit and unlink must name the same inode,
    # so a symlink is never removed here.
    elif [ -L "$path" ]; then
        rm_rc=1
    elif rm -f -- "$path" 2>/dev/null && [ ! -f "$path" ]; then
        rm_rc=0
    else
        rm_rc=1
    fi
    qci_storage_lock_release
    return "$rm_rc"
}

vm_disk_path() {
    local vm=$1 disk
    disk=$("${VIRSH[@]}" domblklist "$vm" --details 2>/dev/null \
        | awk '$2=="disk" && $4 ~ /^\// {print $4; exit}')
    if [ -z "$disk" ]; then
        disk=$("${VIRSH[@]}" domblklist "$vm" --details --inactive 2>/dev/null \
            | awk '$2=="disk" && $4 ~ /^\// {print $4; exit}')
    fi
    [ -n "$disk" ] || return 1
    printf '%s\n' "$disk"
}

# Poll until a domain reaches 'shut off' (or timeout). Returns 0 on success.
wait_for_shutoff() {
    local vm=$1 timeout=${2:-120} i
    for ((i = 0; i < timeout; i++)); do
        [ "$("${VIRSH[@]}" domstate "$vm" 2>/dev/null)" = "shut off" ] && return 0
        sleep 1
    done
    return 1
}

# Build the per-run golden image for a gate family ONCE: spin a full VM (normal
# fresh-vm-bootstrap build of current source), then sync + clean shutdown +
# integrity check, then undefine the domain KEEPING its disk. That disk becomes
# the read-only backing every worker clones from (QCI_RUN_GOLDEN_BACKING), so
# workers skip the build. Idempotent. Returns EXIT_VM_PROVISION on any failure
# (caller should fail the gate fast — no silent fallback to per-worker build).
ensure_run_golden() {
    local profile=$1 spinner gvm gdisk log rc tier2_images=0 gui_session=""
    case "$profile" in
        # Pre-bake the tier-2 podman images into the bats golden so every cloned
        # bats worker inherits them and the tier-2 drivers skip their cold
        # `podman build` hot path (see fresh-vm-bootstrap.sh §8).
        bats) [ -n "$RUN_GOLDEN_BATS" ] && return 0; spinner=spin-test-vm.sh; tier2_images=1 ;;
        gui|gui-admin) [ -n "$RUN_GOLDEN_GUI_ADMIN" ] && return 0; spinner=spin-test-vm-gui.sh; gui_session=labwc; profile=gui-admin ;;
        # Pre-bake the tier-2 podman images into the gui-qdwin golden too: the
        # tier-2 GUI scenarios (permissions-gui/18-podapps, 19-tier5-loopback) run
        # on THIS profile, and without the prebake each one pays the cold
        # `podman build` (≈240s) inside the agent's 720s budget — the dominant
        # cause of their recurring rc=124 agent-timeouts. Built once into the
        # golden, every cloned qdwin worker inherits it and the scenario's
        # `podman image exists` Setup check passes instantly.
        gui-qdwin) [ -n "$RUN_GOLDEN_GUI_QDWIN" ] && return 0; spinner=spin-test-vm-gui.sh; gui_session=qdwin; tier2_images=1 ;;
        *) return 1 ;;
    esac
    log="$RDIR/vm/golden-$profile.log"
    mkdir -p "$(dirname "$log")"
    log "building per-run golden ($profile): one-time compositor build for this run"
    # Bounded wait (H1): the one-time compositor build is the single longest
    # serial step of a run; a wedged build must time out instead of stalling the
    # whole run silently. Generous default (full compositor build + optional tier-2
    # image prebake); override with QCI_GOLDEN_BUILD_TIMEOUT_S. A breach is
    # classified as golden-build infra and recorded as its OWN attempt row (with
    # start/end epochs) so the correlated-burst detector and timing views see it.
    local gb_timeout=${QCI_GOLDEN_BUILD_TIMEOUT_S:-3600} gb_start gb_end
    gb_start=$(date +%s)
    # Full build (QCI_RUN_GOLDEN_BACKING unset => normal bootstrap path).
    timeout "$gb_timeout" env \
        QDWIN_VM_TEMPLATE="${QDWIN_VM_TEMPLATE:-qdistro-template}" \
        QCI_RUN_GOLDEN_BACKING="" \
        QDISTRO_BUILD_TIER2_IMAGES="$tier2_images" \
        QDWIN_APP_DEPS="${QDWIN_APP_DEPS:-0}" \
        QDISTRO_VM_GUI_SESSION="${gui_session:-${QDISTRO_VM_GUI_SESSION:-}}" \
        "$VM_TOOLS/$spinner" "qci-golden-$profile" > "$log" 2>&1
    rc=$?
    gb_end=$(date +%s)
    # Golden build attempt row: golden builds were previously invisible in the
    # attempt ledger (only a result row on failure). Record every build outcome as
    # an attempt so its duration and epochs are visible for p99 tuning + bursts.
    local gb_status=DONE gb_cls=""
    if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then gb_status=TIMEOUT; gb_cls=golden-build
    elif [ "$rc" -ne 0 ]; then gb_status=FAIL; gb_cls=golden-build; fi
    record_attempt "$profile" "golden-build" 1 "$gb_status" "$rc" "$gb_cls" "$((gb_end - gb_start))" "" "$log" "$gb_start" "$gb_end" "golden-$profile"
    if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
        log "golden build ($profile) exceeded ${gb_timeout}s (bounded wait) — classifying golden-build infra"
        record_result "$profile" "golden-build" fail "$EXIT_VM_PROVISION" vm_provision vm "$log" "golden build exceeded ${gb_timeout}s (bounded wait; wedged build or slow disk). Override with QCI_GOLDEN_BUILD_TIMEOUT_S."
        return "$EXIT_VM_PROVISION"
    fi
    if [ "$rc" -ne 0 ]; then
        record_result "$profile" "golden-build" fail "$EXIT_VM_PROVISION" vm_provision vm "$log" "run-golden build failed (rc=$rc)"
        return "$EXIT_VM_PROVISION"
    fi
    gvm=$(grep -E "^qci-golden-$profile-[A-Za-z0-9._-]+$" "$log" | tail -n 1 | tr -d '[:space:]')
    [ -n "$gvm" ] || gvm=$(tail -n 1 "$log" | tr -d '[:space:]')
    if [ -z "$gvm" ]; then
        record_result "$profile" "golden-build" fail "$EXIT_VM_PROVISION" vm_provision vm "$log" "golden spinner produced no VM name"
        return "$EXIT_VM_PROVISION"
    fi
    GOLDEN_INFLIGHT_VMS+=("$gvm")          # so abort_run can reap a half-built golden
    gdisk=$(vm_disk_path "$gvm" || true)
    if [ -z "$gdisk" ]; then
        "${VIRSH[@]}" destroy "$gvm" >/dev/null 2>&1 || true
        "${VIRSH[@]}" undefine "$gvm" --nvram >/dev/null 2>&1 || "${VIRSH[@]}" undefine "$gvm" >/dev/null 2>&1 || true
        record_result "$profile" "golden-build" fail "$EXIT_VM_PROVISION" vm_provision vm "$log" "could not resolve golden disk path"
        return "$EXIT_VM_PROVISION"
    fi
    # Quiesce: sync in-guest, clean shutdown, wait for shut off.
    "$VM_TOOLS/vm-exec" "$gvm" "sync" >/dev/null 2>&1 || true
    "${VIRSH[@]}" shutdown "$gvm" >/dev/null 2>&1 || true
    if ! wait_for_shutoff "$gvm" 180; then
        log "golden $profile did not shut down cleanly within 180s; failing (not using a crash-consistent backing)"
        "${VIRSH[@]}" destroy "$gvm" >/dev/null 2>&1 || true
        "${VIRSH[@]}" undefine "$gvm" --nvram >/dev/null 2>&1 || "${VIRSH[@]}" undefine "$gvm" >/dev/null 2>&1 || true
        safe_rm_overlay "$gdisk" >/dev/null 2>&1 || true
        record_result "$profile" "golden-build" fail "$EXIT_VM_PROVISION" vm_provision vm "$log" "golden did not quiesce"
        return "$EXIT_VM_PROVISION"
    fi
    # Integrity guardrail on the qcow2 metadata before using it as a backing.
    if command -v qemu-img >/dev/null 2>&1 && ! qemu-img check "$gdisk" >/dev/null 2>&1; then
        log "golden $profile failed qemu-img check; failing"
        "${VIRSH[@]}" undefine "$gvm" --nvram >/dev/null 2>&1 || "${VIRSH[@]}" undefine "$gvm" >/dev/null 2>&1 || true
        safe_rm_overlay "$gdisk" >/dev/null 2>&1 || true
        record_result "$profile" "golden-build" fail "$EXIT_VM_PROVISION" vm_provision vm "$log" "golden qemu-img check failed"
        return "$EXIT_VM_PROVISION"
    fi
    # Undefine the domain but KEEP the disk — it is now an immutable backing.
    "${VIRSH[@]}" undefine "$gvm" --nvram >/dev/null 2>&1 || "${VIRSH[@]}" undefine "$gvm" >/dev/null 2>&1 || true
    case "$profile" in
        bats) RUN_GOLDEN_BATS="$gdisk" ;;
        gui-admin) RUN_GOLDEN_GUI_ADMIN="$gdisk" ;;
        gui-qdwin) RUN_GOLDEN_GUI_QDWIN="$gdisk" ;;
    esac
    RUN_GOLDEN_DISKS+=("$gdisk")
    kv "golden_${profile}_disk" "$gdisk"
    # Capability manifest: record what this golden was built WITH, so the gui
    # scheduler (and a human reading artifacts) can tell which app-compatibility
    # scenarios this golden can actually exercise. At minimum the QDWIN_APP_DEPS
    # value — the golden build does not set it, so it inherits the run env (0 by
    # default). A qdwin/tests/apps/* scenario scheduled against an app_deps=0
    # golden is a deterministic SKIP (gui_scenario_app_deps_skip_reason), not an
    # agent dispatch that fails closed on a missing verdict.
    local cap_manifest="$RDIR/vm/golden-$profile.capabilities"
    {
        echo "profile=$profile"
        echo "disk=$gdisk"
        echo "qdwin_app_deps=${QDWIN_APP_DEPS:-0}"
        echo "tier2_images=$tier2_images"
    } > "$cap_manifest"
    kv "golden_${profile}_qdwin_app_deps" "${QDWIN_APP_DEPS:-0}"
    log "per-run golden ($profile) ready: $gdisk (workers will clone from it; qdwin_app_deps=${QDWIN_APP_DEPS:-0})"
    return 0
}

# Report whether a NAME exists inside a directory, distinguishing a real ENOENT
# from a lookup that merely FAILED. Prints `absent`, `present` or `error`.
#
# The shell cannot see errno: `[ -e X ]` and `[ -L X ]` are both false for a
# genuinely missing name AND for EACCES, ELOOP, EIO, ENAMETOOLONG, ESTALE and a
# transient autofs/FUSE lookup failure. Collapsing those into "absent" is how a
# path that DOES resolve to a real image gets recorded as missing, which is how
# an image a domain owns gets unlinked. python3 can see errno, so the decision
# is made there: ONLY ENOENT is absence, every other errno is `error`.
#
# The lookup is performed against an O_DIRECTORY|O_NOFOLLOW fd for the parent,
# so the answer describes the directory INODE we inspected -- replacing the
# parent (with a directory or a symlink) between the caller's checks and this
# one cannot change the answer we already computed, and a parent that has become
# a symlink is `error`, never `absent`. Search permission is enforced by the
# kernel for the fd-relative lstat, so a missing +x surfaces as EACCES -> error.
#
# python3 is a hard requirement of the qci host gate; if it is missing the
# answer is `error` and every caller stays fail-closed.
# A third argument of `follow` asks the same question about the symlink TARGET
# (stat) instead of the link itself (lstat): a demonstrably dangling link is
# `absent`, a link whose target merely cannot be reached is `error`.
QCI_PATH_LOOKUP_PY='
import errno, os, sys
parent, base = sys.argv[1], sys.argv[2]
follow = len(sys.argv) > 3 and sys.argv[3] == "follow"
try:
    fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
except OSError:
    print("error"); raise SystemExit(0)
try:
    try:
        os.stat(base, dir_fd=fd, follow_symlinks=follow)
    except OSError as exc:
        print("absent" if exc.errno == errno.ENOENT else "error")
        raise SystemExit(0)
finally:
    os.close(fd)
print("present")
'
qci_name_lookup_state() {
    local parent=$1 base=$2 follow=${3:-} out
    case "$base" in
        ''|.|..) printf 'error'; return ;;
        */*) printf 'error'; return ;;
    esac
    command -v python3 >/dev/null 2>&1 || { printf 'error'; return; }
    out=$(python3 -c "$QCI_PATH_LOOKUP_PY" "$parent" "$base" "$follow" 2>/dev/null) \
        || { printf 'error'; return; }
    case "$out" in
        absent|present) printf '%s' "$out" ;;
        *) printf 'error' ;;
    esac
}

# Same question for a whole path. Only used where the parent is already known
# to be a real directory (chain walking below); `.`/`..`/`/` are `error`.
qci_path_lookup_state() {
    local path=$1 follow=${2:-} parent base
    [ -n "$path" ] || { printf 'error'; return; }
    parent=$(dirname -- "$path") || { printf 'error'; return; }
    base=$(basename -- "$path") || { printf 'error'; return; }
    qci_name_lookup_state "$parent" "$base" "$follow"
}

# Print the resolved absolute path of $1's IMMEDIATE qcow2 backing file, or the
# sentinel `NONE` (no backing / the file is demonstrably not there, so nothing
# further in the chain can exist) or `ERR` (could not be determined -- callers
# must treat this as "may reference anything").
backing_immediate_parent() {
    local path=$1 info backing
    # Ask qemu-img FIRST: a successful inspection is itself proof the file is
    # there, and it keeps the (comparatively expensive) errno lookup off the
    # hot path -- it runs only when qemu-img could not open the image, which is
    # exactly when "missing" must be distinguished from "could not tell".
    # Bounded, with kill escalation: an unbounded inspection here stalls the
    # whole sweep with the exclusive storage lock held. A timeout leaves `info`
    # empty and non-zero, which falls through to the lookup below and reports
    # `ERR` for a file that exists -- i.e. "could not tell", which KEEPS the
    # image. Fail-closed, not fail-open.
    if ! info=$(timeout -k 5 "${QCI_BACKING_QEMU_IMG_TIMEOUT:-60}" qemu-img info -- "$path" 2>/dev/null); then
        case "$(qci_path_lookup_state "$path")" in
            absent) printf 'NONE' ;;
            *) printf 'ERR' ;;
        esac
        return
    fi
    backing=$(printf '%s\n' "$info" | sed -n 's/^backing file: //p' | head -n1)
    [ -n "$backing" ] || { printf 'NONE'; return; }
    # `backing file: rel.qcow2 (actual path: /abs/rel.qcow2)` -- prefer the
    # actual path qemu itself resolved; fall back to resolving relative to the
    # referrer's directory exactly as qemu would.
    case "$backing" in
        *' (actual path: '*)
            backing=${backing##* (actual path: }
            backing=${backing%)}
            ;;
    esac
    case "$backing" in
        /*) ;;
        *) backing="$(dirname -- "$path")/$backing" ;;
    esac
    backing=$(readlink -m -- "$backing" 2>/dev/null) || { printf 'ERR'; return; }
    [ -n "$backing" ] || { printf 'ERR'; return; }
    printf '%s' "$backing"
}

# --- sweep-spanning immediate-parent cache (ROUND 5) ------------------------
#
# `backing_chain_reaches`'s memo is keyed by node but its ANSWER is
# target-specific, so it can only ever be per-call. That made a whole orphan
# sweep quadratic: every candidate re-ran `qemu-img info` over every image in
# the directory. Measured on a synthetic images directory with one real backing
# chain: N=10 -> 3.16s, N=20 -> 12.08s, N=40 -> 48.17s, and N=78 -- this
# host's real image count -> 164.4s, extrapolating to ~20 minutes at 250. A
# cleanup that slow is a cleanup someone switches off, which is a destructive
# outcome by another road. With the cache, the same N=78 sweep is 8.6s (mean of
# six runs, 8.0-9.1s), a 19x reduction; the nanosecond signature of round 6
# costs about 0.7s of that against round 5's whole-second one.
#
# What IS target-independent is a file's IMMEDIATE backing parent, so that is
# what is cached, keyed by path and validated by a stat signature
# (dev:inode:size:mtime:ctime). A rewrite of the qcow2 header that the
# signature can see -- a `qemu-img rebase`, a re-create, an unlink-and-replace
# -- forces a fresh inspection; a file that has vanished signs as MISSING,
# which never matches; a malformed line is ignored.
#
# ROUND 8 (destructive defect). The signature is NOT a reliable mutation
# detector, and this file no longer claims it is. `qemu-img rebase -u` rewrites
# the header in place, leaving device, inode and size untouched, so the only
# discriminator is the timestamp pair. Whole seconds collided on this host 195
# times in 200 back-to-back rebase pairs. Nanoseconds (`%.9Y`/`%.9Z`) collided
# 0 times in that same experiment -- but "the printed fraction is nonzero" does
# not prove the filesystem issues a NEW ctime for every write. A clock whose
# real granularity is coarser than its printed format can quantise two writes
# into one nonzero tick, and two in-tick, size-preserving rebases then sign
# identically. The round-6 precision gate inferred uniqueness from formatting
# and has been REMOVED rather than left as a disabled guard.
#
# The cache therefore no longer decides anything destructive. It serves the
# CHEAP PRE-SCAN only, where a wrong answer in either direction costs time and
# nothing else:
#   * a wrong `referred` keeps an image that could have gone;
#   * a wrong `clear` only promotes the candidate to the under-lock path, where
#     `backing_referrer_state_authoritative` re-walks every chain with
#     `qemu-img` and consults no cached parent at all.
# Every unlink in the cleanup gate and in cleanup_run_goldens is gated on that
# authoritative answer, taken AFTER the exclusive storage lock is held (see
# qci_storage_lock_acquire), so no cached timestamp authorises a deletion.
#
# What the authoritative pass costs is one `qemu-img info` per distinct image
# reachable from the referrer set, per deletion candidate. It is paid only for
# candidates that have already survived every other keep-rule, so in ORDINARY
# runs -- few condemned images, many reachable ones -- it is a cached scan over
# the directory plus one uncached scan per condemned image.
#
# It is NOT, however, categorically better than the round-4 quadratic: with K
# deletion candidates and N reachable images the authoritative work is O(K*N),
# and when K scales with N that is still quadratic in the worst case. The
# round-8 report's timings (including the 143s figure) are MEASUREMENTS of one
# workload on one host, not a bound: `qemu-img info` inspection is bounded only
# by the per-call timeout below, and ownership-refresh and lock waits add cost
# on top.
#
# The cache is BACKED BY A FILE because destructive callers read this state
# through `$(backing_referrer_state ...)`, a command substitution whose
# subshell discards in-memory state. The file lives in the run directory, so it
# never outlives the run that created it. With no run directory (unit tests,
# ad-hoc calls) caching is in-memory only, which still collapses the repeated
# work inside a single call.
# `-g` is load-bearing: ci/lib/vm.sh is sourced from inside a function by the
# bats harness, where a bare `declare -A` would create a LOCAL array that
# vanishes with the caller, leaving every later subscript reference to be
# evaluated as an arithmetic index and killing the audit mid-answer.
declare -gA _QCI_BACKING_PARENT_CACHE=()
_QCI_BACKING_PARENT_CACHE_LOADED=""

qci_backing_cache_file() {
    if [ -n "${QCI_BACKING_PARENT_CACHE:-}" ]; then
        printf '%s' "$QCI_BACKING_PARENT_CACHE"
    elif [ -n "${RDIR:-}" ] && [ -d "${RDIR:-}/host" ]; then
        printf '%s' "$RDIR/host/backing-parent-cache.tsv"
    fi
}

# Load the on-disk cache into memory once per backing_referrer_state call.
qci_backing_cache_load() {
    local file sig path parent
    file=$(qci_backing_cache_file)
    [ "$_QCI_BACKING_PARENT_CACHE_LOADED" != "${file:-@none@}" ] || return 0
    declare -gA _QCI_BACKING_PARENT_CACHE=()
    _QCI_BACKING_PARENT_CACHE_LOADED="${file:-@none@}"
    [ -n "$file" ] && [ -r "$file" ] || return 0
    while IFS=$'\t' read -r sig path parent; do
        [ -n "$sig" ] && [ -n "$path" ] && [ -n "$parent" ] || continue
        _QCI_BACKING_PARENT_CACHE["$path"]="$sig|$parent"
    done < "$file"
    return 0
}

# Per-call snapshot of `stat` signatures for the enumerated referrer set, so a
# cached scan costs ONE stat(1) rather than one per referrer per candidate.
# Nodes reached by walking a chain outside that set fall back to an individual
# stat. The snapshot is taken at the start of each cached backing_referrer_state
# call and discarded at the end of it. It is not taken at all in authoritative
# mode, which consults no signature.
declare -gA _QCI_BACKING_SIG=()

# The signature format, used by BOTH the per-file and the bulk `stat` paths so
# a cached record and the scan that validates it can never disagree about the
# field layout. The sub-second timestamps make the cache MISS more often on a
# host that records them; they are an optimisation aid, not a correctness
# guarantee, because nothing here establishes that the host issues a distinct
# ctime per write. A coarser format is therefore permitted (a test overrides
# this to stand in for a whole-second filesystem): coarsening it can only make
# the CHEAP pre-scan wrong, and the pre-scan authorises nothing.
QCI_BACKING_SIG_FMT='%d:%i:%s:%.9Y:%.9Z'

# Set to 0 (by backing_referrer_state_authoritative, as a `local`, so the
# setting covers exactly one call) to forbid every cached-parent read and
# write for the duration of one audit.
_QCI_BACKING_TRUST_PARENT_CACHE=1

qci_backing_signature() {
    local sig=${_QCI_BACKING_SIG[$1]:-}
    if [ -n "$sig" ]; then
        printf '%s' "$sig"
        return
    fi
    sig=$(stat -c "$QCI_BACKING_SIG_FMT" -- "$1" 2>/dev/null) || sig=""
    printf '%s' "${sig:-MISSING}"
}

# backing_immediate_parent with the cache in front of it. The answer is
# returned in _QCI_BACKING_PARENT_RESULT rather than printed: a `$(...)` here
# would fork once per chain node per candidate, which is most of the cost this
# cache exists to remove, and would also discard the in-memory cache it just
# populated.
_QCI_BACKING_PARENT_RESULT=""
backing_cached_parent() {
    local path=$1 sig entry parent file
    # Authoritative mode: no cached parent is read and none is written, so the
    # answer below comes from `qemu-img` reading the file as it is now.
    if [ "${_QCI_BACKING_TRUST_PARENT_CACHE:-1}" != 1 ]; then
        _QCI_BACKING_PARENT_RESULT=$(backing_immediate_parent "$path")
        return
    fi
    sig=$(qci_backing_signature "$path")
    entry=${_QCI_BACKING_PARENT_CACHE[$path]:-}
    if [ -n "$entry" ] && [ "$sig" != MISSING ] \
            && [ "${entry%%|*}" = "$sig" ]; then
        _QCI_BACKING_PARENT_RESULT="${entry#*|}"
        return
    fi
    parent=$(backing_immediate_parent "$path")
    _QCI_BACKING_PARENT_RESULT="$parent"
    # Never cache an indeterminate answer, a signature for a file that is not
    # there, or a record whose fields would not survive the tab-separated
    # round trip.
    [ "$parent" != ERR ] || return 0
    [ "$sig" != MISSING ] || return 0
    case "$path$parent" in *$'\t'*|*$'\n'*) return 0 ;; esac
    _QCI_BACKING_PARENT_CACHE["$path"]="$sig|$parent"
    file=$(qci_backing_cache_file)
    [ -n "$file" ] || return 0
    # One short line, written with a single append: atomic enough that a
    # concurrent writer can never interleave a half record.
    printf '%s\t%s\t%s\n' "$sig" "$path" "$parent" >> "$file" 2>/dev/null || true
    return 0
}

# Does the FULL backing chain rooted at $1 contain $2?
#   0 = yes, 1 = provably no, 2 = could not be determined.
# Memoised in _BACKING_REACH_MEMO (declared by backing_referrer_state), so each
# distinct image in a shared chain is inspected once per audit. A cycle or an
# absurd depth is `could not be determined`, never `no`.
backing_chain_reaches() {
    local node=$1 target=$2 parent rc
    case "${_BACKING_REACH_MEMO[$node]:-}" in
        0|1|2) return "${_BACKING_REACH_MEMO[$node]}" ;;
    esac
    if [ -n "${_BACKING_REACH_STACK[$node]:-}" ] \
            || [ "${#_BACKING_REACH_STACK[@]}" -gt 64 ]; then
        return 2
    fi
    _BACKING_REACH_STACK["$node"]=1
    backing_cached_parent "$node"
    parent=$_QCI_BACKING_PARENT_RESULT
    case "$parent" in
        NONE) rc=1 ;;
        ERR)  rc=2 ;;
        *)
            if [ "$parent" = "$target" ]; then
                rc=0
            else
                backing_chain_reaches "$parent" "$target"
                rc=$?
            fi
            ;;
    esac
    unset '_BACKING_REACH_STACK[$node]'
    _BACKING_REACH_MEMO["$node"]=$rc
    return "$rc"
}

# Print `referred`, `clear`, or `unknown` after auditing whether ANY image can
# reach $candidate through its qcow2 backing chain. Unknown is deliberately
# distinct from clear: cleanup must keep a disk when qemu-img is unavailable,
# when a potential referrer cannot be inspected, or when a chain cannot be
# walked to its end.
#
# Round-3 widening (destructive defect). The previous scan looked only at
# `$QDWIN_IMG_DIR/qci-*.qcow2` and only at the IMMEDIATE backing file. Both
# narrowings destroy data:
#   * a human-named `kept.qcow2` (or a golden, or `baseweed-baked.qcow2`) that
#     backs onto an old `qci-*.qcow2` was never examined at all, so the
#     candidate read `clear` and the orphan sweep unlinked a live backing file,
#     corrupting `kept.qcow2`;
#   * `kept.qcow2 -> mid.qcow2 -> qci-old.qcow2` hides the candidate one level
#     down, so even a widened immediate-parent scan would have read `clear`.
# Every regular file in the images directory is now a potential referrer, and
# the whole chain is walked. Callers that know of images OUTSIDE that directory
# (cleanup passes every canonical disk libvirt declares, wherever it lives) add
# them through BACKING_REFERRER_EXTRA_LIST, a newline-separated file of paths.
#
# Cost: one `qemu-img info` per DISTINCT image reachable from the directory
# that the immediate-parent cache does not already answer, memoised across the
# whole audit call (~25 ms each; ~2.0 s for a 78-file images directory on the
# reference host when nothing is cached).
#
# This entry point may consult that cache and therefore MUST NOT be used to
# authorise an unlink. Destructive callers use
# backing_referrer_state_authoritative below.
backing_referrer_state() {
    local candidate=$1 img_dir ov ov_real candidate_real extra line
    local _sig_path _sig_val
    local -A _BACKING_REACH_MEMO=() _BACKING_REACH_STACK=()
    local -a referrers=()
    [ -n "$candidate" ] || { printf 'unknown'; return; }
    command -v qemu-img >/dev/null 2>&1 || { printf 'unknown'; return; }
    command -v python3 >/dev/null 2>&1 || { printf 'unknown'; return; }
    img_dir="${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}"
    candidate_real=$(readlink -m -- "$candidate" 2>/dev/null) \
        || { printf 'unknown'; return; }
    [ -n "$candidate_real" ] || { printf 'unknown'; return; }
    if [ "${_QCI_BACKING_TRUST_PARENT_CACHE:-1}" = 1 ]; then
        qci_backing_cache_load
    fi

    # Enumerate the images directory (dotfiles included) plus any caller-
    # supplied out-of-directory referrers. A directory we cannot even list is
    # `unknown`: an unlistable referrer set is not an empty one.
    if [ ! -d "$img_dir" ] || [ ! -r "$img_dir" ] || [ ! -x "$img_dir" ]; then
        printf 'unknown'
        return
    fi
    # ROUND 5: built as an ARRAY rather than piped into a `{ ... }` block. The
    # pipeline made the loop a subshell, which silently discarded every
    # immediate-parent cache entry it learned.
    # `-L` as well as `-e`: a symlink whose target cannot be reached is
    # exactly the entry that must be classified, not dropped.
    # Enumerate from the CANONICAL images directory. A plain file directly
    # inside it is then already its own canonical path, which removes a
    # readlink(1) fork per entry per candidate; a symlink, or anything from the
    # caller-supplied extra list, still goes through the full resolution below.
    local img_dir_real
    img_dir_real=$(readlink -e -- "$img_dir" 2>/dev/null) \
        || { printf 'unknown'; return; }
    local -a ref_canonical=()
    for ov in "$img_dir_real"/* "$img_dir_real"/.[!.]* "$img_dir_real"/..?*; do
        if [ -e "$ov" ] || [ -L "$ov" ]; then
            referrers+=("$ov")
            if [ -L "$ov" ] || [ ! -e "$ov" ]; then
                ref_canonical+=(0)
            else
                ref_canonical+=(1)
            fi
        fi
    done
    extra="${BACKING_REFERRER_EXTRA_LIST:-}"
    if [ -n "$extra" ]; then
        # An extra-referrer list that was requested but cannot be read is an
        # unknown referrer set, never an empty one.
        [ -r "$extra" ] || { printf 'unknown'; return; }
        while IFS= read -r line; do
            [ -n "$line" ] || continue
            referrers+=("$line")
            ref_canonical+=(0)
        done < "$extra"
    fi

    # One stat(1) for the whole referrer set feeds the cache-validation
    # signatures (see qci_backing_signature). Authoritative mode validates no
    # cache entry, so it does not pay for this and does not populate it.
    declare -gA _QCI_BACKING_SIG=()
    if [ "${_QCI_BACKING_TRUST_PARENT_CACHE:-1}" = 1 ] \
            && [ "${#referrers[@]}" -gt 0 ]; then
        # Signature FIRST so a pathname containing '|' still round-trips: the
        # last `read` field absorbs the remainder of the line.
        while IFS='|' read -r _sig_val _sig_path; do
            [ -n "$_sig_path" ] && [ -n "$_sig_val" ] || continue
            _QCI_BACKING_SIG["$_sig_path"]="$_sig_val"
        done < <(stat -c "$QCI_BACKING_SIG_FMT|%n" -- "${referrers[@]}" 2>/dev/null)
    fi

    local seen_unknown=0 i n=${#referrers[@]}
    for ((i = 0; i < n; i++)); do
        line=${referrers[i]}
        [ -n "$line" ] || continue
        # A directory is never a qcow2 referrer. Anything else that
        # RESOLVES is inspected. Anything that does not resolve is only
        # skipped when it is demonstrably not there (ENOENT on the target);
        # a link we merely cannot follow could name a real image, so it is
        # the ambiguity that must fail closed, not a silent skip.
        if [ -d "$line" ]; then
            continue
        fi
        if [ "${ref_canonical[i]}" = 1 ]; then
            ov_real=$line
        elif [ -e "$line" ]; then
            ov_real=$(readlink -e -- "$line" 2>/dev/null) \
                || { seen_unknown=1; break; }
        else
            # Treating local absence as "not a referrer" is only sound because
            # every path in this list was resolved in OUR filesystem
            # namespace: cleanup_uri_is_local() fails a remote connection
            # closed rather than publishing its paths, so a remote guest's
            # shared storage mounted at a different path can never arrive here
            # and be dismissed by a local ENOENT.
            case "$(qci_path_lookup_state "$line" follow)" in
                absent) continue ;;
                *) seen_unknown=1; break ;;
            esac
        fi
        [ "$ov_real" = "$candidate_real" ] && continue
        backing_chain_reaches "$ov_real" "$candidate_real"
        case $? in
            0) printf 'referred'; return ;;
            2) seen_unknown=1; break ;;
        esac
    done
    [ "$seen_unknown" = 0 ] || { printf 'unknown'; return; }
    _QCI_BACKING_SIG=()
    printf 'clear'
}

# The audit that a deletion is allowed to rest on.
#
# Identical to backing_referrer_state except that every immediate-parent answer
# comes from `qemu-img` reading the file as it is at this moment: no entry is
# read from the sweep-spanning parent cache and none is written to it, and no
# stat signature is consulted, so no timestamp -- of any precision -- takes
# part in the decision. The `local` is deliberate: bash's dynamic scoping makes
# it visible to backing_cached_parent for exactly the duration of this call.
#
# What this buys, precisely: the answer reflects the on-disk backing chains as
# read by `qemu-img` during this call. It is NOT a claim that nothing can
# change afterwards. That part is the CALLER's, and it holds only where the
# caller arranges it. The three callers that exist today -- the cleanup gate's
# two unlink sites and cleanup_run_goldens -- each take the exclusive storage
# lock BEFORE calling this and release it only AFTER the unlink, so for that
# whole window every other qci writer (clone-baseweed.sh's create..define
# window, every safe_rm_overlay) is excluded. A writer that does not take the
# storage lock -- anything outside qci touching the same images directory -- is
# outside that protocol and outside this guarantee.
#
# Note also what this function is NOT: it is not wired into safe_rm_overlay, so
# the other unlink routes (release_vm's leaked-overlay reclaim, reap_new_orphans,
# the golden-build failure paths) still delete overlays they created without a
# backing-chain audit of any kind. That is unchanged by round 8.
backing_referrer_state_authoritative() {
    local _QCI_BACKING_TRUST_PARENT_CACHE=0
    backing_referrer_state "$1"
}

# Compatibility predicate used by the H12 contract tests and callers that only
# need the positive case. It reads the CACHED state and must never gate an
# unlink; destructive callers use backing_referrer_state_authoritative and
# preserve on `unknown`.
golden_has_backing_referrer() {
    [ "$(backing_referrer_state "$1")" = referred ]
}

# Remove golden backing disks at end of run — but ONLY once no worker overlay
# can still reference them. If a failed worker was PRESERVED for triage, its
# overlay's backing chain points at the golden, so we keep the golden too.
cleanup_run_goldens() {
    local d ref_state
    for d in "${RUN_GOLDEN_DISKS[@]:-}"; do
        [ -n "$d" ] || continue
        # Authoritative signal is the marker FILE written by release_vm (it
        # survives the backgrounded worker subshells where the var cannot); the
        # GOLDEN_PRESERVE var is an in-parent fallback (abort_run path).
        if [ "$GOLDEN_PRESERVE" = 1 ] || [ -e "$RDIR/vm/golden-preserve" ]; then
            log "preserving golden backing $d (a failed worker overlay was preserved and references it)"
            printf 'preserved_golden_disk=%s\n' "$d" >> "$RDIR/manifest.txt"
            continue
        fi
        # ROUND 5 (destructive defect 4): the referrer audit and the unlink
        # must be ONE critical section, exactly as in the cleanup gate. Taking
        # the lock only inside safe_rm_overlay would still let a worker start a
        # clone backed onto this golden in the window between the audit
        # returning `clear` and the unlink. Fail closed: a golden we cannot
        # lock is preserved, never deleted.
        if ! qci_storage_lock_acquire; then
            log "preserving golden backing $d (${QCI_STORAGE_LOCK_REASON:-storage lock unavailable})"
            printf 'preserved_golden_disk=%s\n' "$d" >> "$RDIR/manifest.txt"
            continue
        fi
        # H12 belt-and-suspenders: the marker write can fail inside the worker
        # subshell (disk full/unwritable), which would otherwise let us delete a
        # golden a preserved overlay still backs. Independently verify no
        # surviving overlay references this golden as backing before deleting it.
        # Authoritative: this answer authorises an unlink, so it may not come
        # from the cache. The lock is already held, above.
        ref_state=$(backing_referrer_state_authoritative "$d")
        if [ "$ref_state" != clear ]; then
            qci_storage_lock_release
            log "preserving golden backing $d (backing-referrer audit: $ref_state)"
            printf 'preserved_golden_disk=%s\n' "$d" >> "$RDIR/manifest.txt"
            continue
        fi
        if safe_rm_overlay "$d"; then
            log "removed per-run golden $d"
        fi
        qci_storage_lock_release
    done
}

collect_vm_artifacts() {
    local vm=$1 label=${2:-vm} outdir vmx
    [ -n "$vm" ] || return 0
    outdir="$RDIR/vm/$label"
    mkdir -p "$outdir" "$RDIR/journals" "$RDIR/screenshots"
    "${VIRSH[@]}" dumpxml "$vm" > "$outdir/domain.xml" 2>&1 || true
    "${VIRSH[@]}" domblklist "$vm" --details > "$outdir/domblklist.txt" 2>&1 || true
    "${VIRSH[@]}" dominfo "$vm" > "$outdir/dominfo.txt" 2>&1 || true
    "${VIRSH[@]}" screenshot "$vm" "$RDIR/screenshots/$label-final.ppm" >/dev/null 2>&1 || true
    vmx="$VM_TOOLS/vm-exec"
    [ -x "$vmx" ] || return 0
    # A SHARED LINE CAP IS THE WRONG UNIT, and this is the second time it has
    # cost an investigation. The cap was already widened 400 -> 3000 after it
    # truncated the scenario-19 VT-takeaway evidence
    # (todo/screenshots/README.md). In gui-20260919T072913Z, scenario 24's
    # frame went black at 07:55:14 and a crash-looping qdlocker (restart
    # counter 121, ~9 lines every 2s) plus spice-vdagent had already flooded
    # the buffer: the captured user journal was exactly 3001 lines and began
    # at 07:57. The decisive minute was discarded by the collector, not
    # missing from the guest.
    #
    # Widening again only buys time until something loops faster. The
    # structural fix is that ONE NOISY UNIT MUST NOT EVICT THE LINES OF
    # ANOTHER, so
    # the units that explain a display failure are captured into their own
    # files, each with its own budget, in ADDITION to the whole-journal tails.
    "$vmx" "$vm" "journalctl -b --no-pager 2>/dev/null | tail -8000" > "$RDIR/journals/$label-system.log" 2>&1 || true
    "$vmx" "$vm" "journalctl _UID=1000 -b --no-pager 2>/dev/null | tail -8000" > "$RDIR/journals/$label-user-1000.log" 2>&1 || true
    "$vmx" "$vm" "journalctl -b -u seatd -u systemd-logind -u '"'"'getty@*'"'"' --no-pager 2>/dev/null" > "$RDIR/journals/$label-seat-vt.log" 2>&1 || true
    # ONE FILE AND ONE BUDGET PER UNIT. A combined query behind a shared
    # `tail` would not fix anything -- the locker could still consume the whole
    # window and bury the compositor, just with a different number on it. Each
    # unit gets its own file so no unit's volume can evict another's lines.
    local dunit dslug
    for dunit in qdwin-compositor.service qdshell.service qdlocker.service; do
        dslug=${dunit%.service}
        "$vmx" "$vm" "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 journalctl --user -b -u $dunit --no-pager 2>/dev/null | tail -4000" > "$RDIR/journals/$label-unit-$dslug.log" 2>&1 || true
    done
    # Merged chronology as well, because interleaving across the three is what
    # shows a handoff failing. This one is a convenience and may be truncated
    # by a noisy neighbour; the per-unit files above are isolated bounded
    # histories -- a unit can still evict its OWN earlier lines, including the
    # start of its own loop, but it cannot evict another unit's.
    "$vmx" "$vm" "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 journalctl --user -b -u qdwin-compositor.service -u qdshell.service -u qdlocker.service --no-pager 2>/dev/null | tail -4000" > "$RDIR/journals/$label-display-units.log" 2>&1 || true
    # WHO IS FLOODING: line counts per syslog identifier, most-noisy first. A
    # crash-loop is invisible in a truncated tail but obvious as a count, and
    # this is the line that would have named qdlocker immediately. It is a
    # TOP-TALKERS HEURISTIC, not exhaustive attribution: entries carrying no
    # SYSLOG_IDENTIFIER are not counted at all, and the extraction is a regex
    # over one-line JSON rather than a JSON parse: JSON escapes are not
    # decoded, and because the match stops at the first quote, an identifier
    # containing an ESCAPED quote is truncated (`a\"b` is reported as `a\`).
    # Good enough to name a loop. Base64ed
    # rather than inlined: the pipeline needs nested single and double quotes,
    # and building that as a shell string inside a shell string is how the
    # first version of this line came out unparseable.
    local flood_b64 flood_script
    flood_script=$(cat <<'FLOODEOF'
journalctl -b --no-pager -o json --output-fields=SYSLOG_IDENTIFIER 2>/dev/null \
  | grep -ao '"SYSLOG_IDENTIFIER"[[:space:]]*:[[:space:]]*"[^"]*"' \
  | sed 's/.*"\([^"]*\)"$/\1/' \
  | sort | uniq -c | sort -rn | head -40
FLOODEOF
)
    flood_b64=$(printf '%s' "$flood_script" | base64 -w0 2>/dev/null) \
        || flood_b64=$(printf '%s' "$flood_script" | base64 | tr -d '\n')
    "$vmx" "$vm" "printf '%s' '$flood_b64' | base64 -d | bash" > "$RDIR/journals/$label-flood-report.txt" 2>&1 || true
    "$vmx" "$vm" "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user status qdwin-compositor.service qdshell.service qdlocker.service qdistro-cursor-sprites.service --no-pager 2>/dev/null || true" > "$outdir/systemctl-user-status.txt" 2>&1 || true
    "$vmx" "$vm" "WAYLAND_DISPLAY=wayland-1 XDG_RUNTIME_DIR=/run/user/1000 runuser -u admin -- wayland-info 2>/dev/null | head -240 || true" > "$outdir/wayland-info.txt" 2>&1 || true
    "$vmx" "$vm" "echo list | socat - UNIX-CONNECT:/run/user/1000/qdshell.sock 2>&1 | head -200 || true" > "$outdir/qdshell-list.txt" 2>&1 || true
}
