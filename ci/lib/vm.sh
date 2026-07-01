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
    t_start=$(date +%s)
    timeout "$prov_timeout" env \
        QDWIN_VM_TEMPLATE="${QDWIN_VM_TEMPLATE:-qdistro-template}" \
        QCI_RUN_GOLDEN_BACKING="$golden_backing" \
        QDISTRO_VM_GUI_SESSION="${gui_session:-${QDISTRO_VM_GUI_SESSION:-}}" \
        "$VM_TOOLS/$spinner" "qci-$gate" > "$log_path" 2>&1
    rc=$?
    t_end=$(date +%s)
    if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
        log "acquire_vm: $spinner for $gate exceeded ${prov_timeout}s (bounded wait) — classifying vm_provision infra"
        record_attempt "$gate" "$spinner" 1 TIMEOUT "$rc" vm-provision "$((t_end - t_start))" "" "$log_path" "$t_start" "$t_end" "$gate"
        record_result "$gate" "$spinner" fail "$EXIT_VM_PROVISION" vm_provision vm "$log_path" "VM provisioning exceeded ${prov_timeout}s (bounded wait; likely wedged spinner or slow disk). Override with QCI_VM_PROVISION_TIMEOUT_S."
        return "$EXIT_VM_PROVISION"
    fi
    if [ "$rc" -ne 0 ]; then
        record_result "$gate" "$spinner" fail "$EXIT_VM_PROVISION" vm_provision vm "$log_path" "VM creation failed"
        return "$EXIT_VM_PROVISION"
    fi
    vm=$(grep -E "^qci-[A-Za-z0-9._-]+$" "$log_path" | tail -n 1 | tr -d '[:space:]')
    [ -n "$vm" ] || vm=$(tail -n 1 "$log_path" | tr -d '[:space:]')
    if [ -z "$vm" ]; then
        record_result "$gate" "$spinner" fail "$EXIT_VM_PROVISION" vm_provision vm "$log_path" "$spinner produced no VM name"
        return "$EXIT_VM_PROVISION"
    fi
    CREATED_VMS+=("$vm")
    printf '%s\n' "$vm" >> "$RDIR/vm/created-vms.txt"
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
            mkdir -p "$RDIR/vm" && : > "$RDIR/vm/golden-preserve"
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

# Guarded removal of a qci disposable overlay. Only removes a path that is a
# regular non-empty file, lives directly under the libvirt images dir, and
# whose basename starts with `qci-` (so backing images like baseweed-baked.qcow2
# or qdistro-daily* are never touched). Returns 0 only if it removed the file.
safe_rm_overlay() {
    local path=$1
    [ -n "$path" ] || return 1
    [ -f "$path" ] || return 1
    local img_dir base
    img_dir="${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}"
    base=$(basename -- "$path")
    case "$base" in
        qci-*) ;;
        *) return 1 ;;
    esac
    # Must reside directly in the images dir (not a symlink/escape).
    [ "$path" = "$img_dir/$base" ] || return 1
    rm -f -- "$path" 2>/dev/null && [ ! -f "$path" ]
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

# Remove golden backing disks at end of run — but ONLY once no worker overlay
# can still reference them. If a failed worker was PRESERVED for triage, its
# overlay's backing chain points at the golden, so we keep the golden too.
cleanup_run_goldens() {
    local d
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
        if safe_rm_overlay "$d"; then
            log "removed per-run golden $d"
        fi
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
    "$vmx" "$vm" "journalctl -b --no-pager 2>/dev/null | tail -400" > "$RDIR/journals/$label-system.log" 2>&1 || true
    "$vmx" "$vm" "journalctl _UID=1000 -b --no-pager 2>/dev/null | tail -400" > "$RDIR/journals/$label-user-1000.log" 2>&1 || true
    "$vmx" "$vm" "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user status qdwin-compositor.service qdshell.service qdlocker.service qdistro-cursor-sprites.service --no-pager 2>/dev/null || true" > "$outdir/systemctl-user-status.txt" 2>&1 || true
    "$vmx" "$vm" "WAYLAND_DISPLAY=wayland-1 XDG_RUNTIME_DIR=/run/user/1000 runuser -u admin -- wayland-info 2>/dev/null | head -240 || true" > "$outdir/wayland-info.txt" 2>&1 || true
    "$vmx" "$vm" "echo list | socat - UNIX-CONNECT:/run/user/1000/qdshell.sock 2>&1 | head -200 || true" > "$outdir/qdshell-list.txt" 2>&1 || true
}
