#!/usr/bin/env bash
# qci module: image gate
# Extracted verbatim from bin/qci. SOURCED by bin/qci into the single
# CI-runner process (shared RDIR/CREATED_VMS/golden state/traps); it is
# NOT executed standalone. See ci/AGENTS.md for the module map.
# shellcheck shell=bash

# ---------------------------------------------------------------------------
# qci image gate. (a) static image-content checklist first (fail fast,
# no VM); then (b) boot-verify + install-test. The boot/install stages
# need a built image + libvirt + a VM, so they degrade to record_blocked
# with a clear "needs VM/image" message when prerequisites are absent.
# ---------------------------------------------------------------------------
gate_image() {
    qci_assert_run_dir || return $?
    local root="" idempotency=0 no_boot=0 rc=$EXIT_OK
    while [ $# -gt 0 ]; do
        case "$1" in
            --root) shift; root=${1:-} ;;
            --idempotency) idempotency=1 ;;
            --no-boot) no_boot=1 ;;
            *) record_blocked image "$1" "$EXIT_USAGE" args "unknown image flag"; return "$EXIT_USAGE" ;;
        esac
        shift
    done

    local checker="$IMAGE_DIR/verify-contents.sh"
    local build_dir="${QDISTRO_BUILD_DIR:-/tmp/qdistro-build}"
    kv image_build_dir "$build_dir"
    [ -n "$root" ] && kv image_static_root "$root"

    # --- Stage A: static image-content checklist (fail fast) ----------------
    if [ ! -x "$checker" ] && [ ! -f "$checker" ]; then
        record_blocked image verify-contents "$EXIT_PREFLIGHT" image "image/verify-contents.sh missing"
        return "$EXIT_PREFLIGHT"
    fi
    # Resolve the tree to inspect: explicit --root, else an extracted tree
    # under the build dir, else nothing (boot-only build present).
    local static_root="$root"
    if [ -z "$static_root" ]; then
        for cand in "$build_dir/extracted" "$build_dir/root" "$build_dir/mnt"; do
            [ -d "$cand" ] && { static_root="$cand"; break; }
        done
    fi
    # Run the checker whenever the user EXPLICITLY passed --root (even a bad
    # path: verify-contents.sh returns 2 for a missing/non-dir root, which must
    # surface as a FAIL, not be masked as a non-failing record_blocked). Only
    # the auto-discovery-found-nothing case is a legitimate "blocked, needs an
    # extracted tree" — never a user-supplied root.
    if [ -n "$root" ] || { [ -n "$static_root" ] && [ -d "$static_root" ]; }; then
        local sc_log="$RDIR/host/image-verify-contents.log"
        mkdir -p "$(dirname "$sc_log")"
        log "image: static content checklist against ${static_root:-<unset>}"
        bash "$checker" "$static_root" > "$sc_log" 2>&1
        local sc_rc=$?
        if [ "$sc_rc" -eq 0 ]; then
            record_result image verify-contents pass 0 pass image "$sc_log" "static checklist passed ($static_root)"
        else
            record_result image verify-contents fail "$EXIT_BUILD" build image "$sc_log" "static checklist failed rc=$sc_rc ($static_root)"
            rc=$EXIT_BUILD
            # Fail fast: do not boot a tree that failed static inspection.
            return "$rc"
        fi
    else
        record_blocked image verify-contents "$EXIT_BUILD" image \
            "no extracted image tree to inspect; pass --root <dir> or extract under $build_dir (needs built image)"
    fi

    if [ "$no_boot" = 1 ]; then
        log "image: --no-boot set; skipping boot/install stages"
        return "$rc"
    fi

    # --- Stage B: boot-verify + install-test (needs VM + built image) -------
    # These are NOT runnable without libvirt and a built artifact. Guard each
    # and degrade to record_blocked with a precise reason.
    local have_image=0 img
    img=$(find "$build_dir" -maxdepth 2 \( -name '*.raw' -o -name '*.qcow2' \) 2>/dev/null | head -1)
    [ -n "$img" ] && have_image=1
    local have_virsh=0
    command -v virsh >/dev/null 2>&1 && "${VIRSH[@]}" list >/dev/null 2>&1 && have_virsh=1

    if [ "$have_image" = 0 ] || [ "$have_virsh" = 0 ]; then
        local why="needs VM/image:"
        [ "$have_image" = 0 ] && why="$why no built image in $build_dir;"
        [ "$have_virsh" = 0 ] && why="$why libvirt session unavailable;"
        record_blocked image verify.sh "$EXIT_VM_PROVISION" image "$why run image/build-in-vm.sh on a test machine"
        record_blocked image install-test.sh "$EXIT_VM_PROVISION" image "$why run image/build-in-vm.sh on a test machine"
        if [ "$idempotency" = 1 ]; then
            record_blocked image install-test.sh-2nd "$EXIT_VM_PROVISION" image "$why idempotency (double-install) needs a built image + VM"
        fi
        return "$rc"
    fi

    # Prerequisites present: run the existing boot/install flow.
    local v_log="$RDIR/host/image-verify.log"
    log "image: boot-verify (image/verify.sh)"
    bash "$IMAGE_DIR/verify.sh" > "$v_log" 2>&1
    local v_rc=$?
    if [ "$v_rc" -eq 0 ]; then
        record_result image verify.sh pass 0 pass image "$v_log" "boot-verify passed"
    else
        record_result image verify.sh fail "$EXIT_VM_BOOT" vm_boot image "$v_log" "boot-verify failed rc=$v_rc"
        [ "$rc" -eq 0 ] && rc=$EXIT_VM_BOOT
    fi

    local i_log="$RDIR/host/image-install.log"
    log "image: install-test (image/install-test.sh)"
    bash "$IMAGE_DIR/install-test.sh" > "$i_log" 2>&1
    local i_rc=$?
    # Trust the exit code, but also fail on a "RESULT: FAIL" line in the log:
    # install-test.sh historically printed RESULT: FAIL and still exited 0, which
    # would record a real install failure as a pass. Defense in depth so a future
    # fall-through can't mask a failure here either.
    if [ "$i_rc" -eq 0 ] && ! grep -q "RESULT: FAIL" "$i_log" 2>/dev/null; then
        record_result image install-test.sh pass 0 pass image "$i_log" "install + reboot passed"
    else
        grep -q "RESULT: FAIL" "$i_log" 2>/dev/null && [ "$i_rc" -eq 0 ] && i_rc="RESULT:FAIL(exit0)"
        record_result image install-test.sh fail "$EXIT_VM_BOOT" vm_boot image "$i_log" "install-test failed rc=$i_rc"
        [ "$rc" -eq 0 ] && rc=$EXIT_VM_BOOT
    fi

    if [ "$idempotency" = 1 ]; then
        # Double-install idempotency: a clean second install over the
        # already-installed target. install-test.sh wipes+reinstalls its
        # own target, so re-invoking it exercises a fresh install; the
        # idempotency assertion is that the second run is ALSO clean.
        local i2_log="$RDIR/host/image-install-2nd.log"
        log "image: idempotency — second install pass"
        bash "$IMAGE_DIR/install-test.sh" > "$i2_log" 2>&1
        local i2_rc=$?
        if [ "$i2_rc" -eq 0 ] && ! grep -q "RESULT: FAIL" "$i2_log" 2>/dev/null; then
            record_result image install-test.sh-2nd pass 0 pass image "$i2_log" "second install clean (idempotent)"
        else
            grep -q "RESULT: FAIL" "$i2_log" 2>/dev/null && [ "$i2_rc" -eq 0 ] && i2_rc="RESULT:FAIL(exit0)"
            record_result image install-test.sh-2nd fail "$EXIT_VM_BOOT" vm_boot image "$i2_log" "second install not clean rc=$i2_rc"
            [ "$rc" -eq 0 ] && rc=$EXIT_VM_BOOT
        fi
    fi

    return "$rc"
}
