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
    local build_dir="${QDISTRO_BUILD_DIR:-/var/tmp/qdistro-build}"
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
    # A built .raw and no explicit --root: ALWAYS extract the checklist's
    # paths from THAT raw (image/extract-root.sh, guestfish copy-out; seconds,
    # ~100 MB) into $build_dir/extracted, replacing whatever was there. An
    # older extracted tree must never be inspected in place of a newer raw
    # (Phase B review): a rebuilt-broken raw next to yesterday's clean tree
    # would otherwise pass.
    local raw="" nraws
    nraws="$(ls "$build_dir"/*.raw 2>/dev/null | wc -l)"
    if [ "$nraws" -gt 1 ] && [ -z "$static_root" ]; then
        # Two raws make "the built artifact" ambiguous; never pick one by name.
        record_result image extract-root fail "$EXIT_BUILD" build image "" "$nraws .raw files under $build_dir; cannot tell which was built -- pass --root or remove the stale one"
        return "$EXIT_BUILD"
    fi
    [ "$nraws" = 1 ] && raw="$(ls "$build_dir"/*.raw)"
    # `extracted_fresh` is the FACT that Stage B keys on (not the path string
    # of $static_root, which a stale fallback tree would also satisfy).
    local extracted_fresh=0
    if [ -z "$static_root" ] && [ -n "$raw" ] && [ -f "$IMAGE_DIR/extract-root.sh" ]; then
        local ex_log="$RDIR/host/image-extract-root.log"
        mkdir -p "$(dirname "$ex_log")"
        log "image: extracting checklist paths from $raw"
        if QDISTRO_BUILD_DIR="$build_dir" bash "$IMAGE_DIR/extract-root.sh" "$raw" > "$ex_log" 2>&1; then
            static_root="$build_dir/extracted"
            extracted_fresh=1
        else
            record_result image extract-root fail "$EXIT_BUILD" build image "$ex_log" "could not extract the built raw for inspection (image/extract-root.sh needs guestfish/libguestfs; see $ex_log)"
            return "$EXIT_BUILD"
        fi
    fi
    # No raw: fall back to a pre-extracted tree if one exists.
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
    # The artifact to boot is the ONE the static stage selected ($raw, the
    # sole top-level .raw). Re-discovering it here with a broader find could
    # boot a stale qcow2 or nested raw while the static stage judged another
    # file (Phase B review); verify.sh is told the exact path.
    # Boot ONLY the artifact Stage A inspected: the sole top-level raw that
    # extract-root.sh just unpacked. With --root, or with a pre-extracted
    # fallback tree, there is no provable link between the inspected tree
    # and any bootable file, so booting one would judge two different
    # artifacts as if they were one (round-4 review); that case is BLOCKED
    # with the reason, not silently booted.
    local img=""
    if [ -z "$root" ] && [ -n "$raw" ] && [ "$extracted_fresh" = 1 ]; then
        img="$raw"
    fi
    if [ -z "$img" ]; then
        local why="boot needs the built raw Stage A inspected:"
        [ -n "$root" ] && why="$why --root was given, so the inspected tree has no provable source image;"
        [ -z "$raw" ] && why="$why no single top-level .raw under $build_dir;"
        record_blocked image verify.sh "$EXIT_VM_PROVISION" image "$why run image/build-in-vm.sh and rerun without --root"
        record_blocked image install-test.sh "$EXIT_VM_PROVISION" image "$why"
        if [ "$idempotency" = 1 ]; then
            record_blocked image install-test.sh-2nd "$EXIT_VM_PROVISION" image "$why"
        fi
        return "$rc"
    fi
    local have_virsh=0
    command -v virsh >/dev/null 2>&1 && "${VIRSH[@]}" list >/dev/null 2>&1 && have_virsh=1

    if [ "$have_virsh" = 0 ]; then
        local why="needs VM: libvirt session unavailable;"
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
    QDISTRO_IMAGE="$img" bash "$IMAGE_DIR/verify.sh" > "$v_log" 2>&1
    local v_rc=$?
    if [ "$v_rc" -eq 0 ]; then
        record_result image verify.sh pass 0 pass image "$v_log" "boot-verify passed"
    else
        record_result image verify.sh fail "$EXIT_VM_BOOT" vm_boot image "$v_log" "boot-verify failed rc=$v_rc"
        [ "$rc" -eq 0 ] && rc=$EXIT_VM_BOOT
    fi

    # The tester build is the raw alone: config.xml sets installiso="false"
    # (todo/iso/13), so no .install.iso exists next to the raw and there is
    # nothing for install-test.sh to boot. That is the designed shape of the
    # artifact, not a missing prerequisite: record it as a skip with the
    # reason, and only run the install stages when an ISO from THIS build
    # (same directory as the raw) is present.
    local iso
    iso="$(ls "$build_dir"/*.install.iso 2>/dev/null | head -1)"
    if [ -z "$iso" ]; then
        local why="no .install.iso next to $img: the tester image is built with installiso=false (todo/iso/13), so install-test.sh (and --idempotency, which re-runs it) is inert until the post-v1 installable ISO returns; not a missing prerequisite"
        record_skip image install-test.sh image "$why"
        if [ "$idempotency" = 1 ]; then
            record_skip image install-test.sh-2nd image "$why"
        fi
        return "$rc"
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
