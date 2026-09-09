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
    # Resolve the tree to inspect: explicit --root, else extract from the
    # published artifact. The tester download is bundle/*.raw.xz + .sha256
    # (todo/iso/14 Phase E item 6): verify the digest, decompress, and
    # inspect THAT raw. A top-level *.raw is the fallback when no bundle
    # exists (a half-copied build). Never `find | head -1`.
    local static_root="$root"
    # `extracted_fresh` is the FACT that Stage B keys on (not the path string
    # of $static_root, which a stale fallback tree would also satisfy).
    local extracted_fresh=0 raw="" published=""
    if [ -z "$static_root" ] && [ -f "$IMAGE_DIR/lib/select-artifact.sh" ]; then
        # shellcheck source=../../../image/lib/select-artifact.sh
        . "$IMAGE_DIR/lib/select-artifact.sh"
        local sel_log="$RDIR/host/image-select-artifact.log"
        mkdir -p "$(dirname "$sel_log")"
        if QDISTRO_BUILD_DIR="$build_dir" qdistro_resolve_image >"$sel_log" 2>&1 \
           && QDISTRO_BUILD_DIR="$build_dir" qdistro_materialize_raw >>"$sel_log" 2>&1; then
            published="$QDISTRO_RESOLVED_PATH"
            raw="$QDISTRO_RESOLVED_DISK"
            kv image_published "$published"
            kv image_resolved_kind "${QDISTRO_RESOLVED_KIND:-}"
            kv image_digest "${QDISTRO_RESOLVED_DIGEST:-}"
        else
            # A present-but-bad artifact (checksum mismatch, two xz, two
            # raws, xz -t fail) must FAIL, not fall through to yesterday's
            # extracted tree (iso/14 Phase E independent B1). Only "no
            # files at all" may use the pre-extracted fallback.
            local n_xz n_raw
            n_xz="$(find "$build_dir/bundle" -maxdepth 1 -name '*.raw.xz' -type f 2>/dev/null | wc -l)"
            n_raw="$(find "$build_dir" -maxdepth 1 -name '*.raw' -type f 2>/dev/null | wc -l)"
            if [ -n "${QDISTRO_IMAGE:-}" ] || [ "${n_xz:-0}" -ge 1 ] || [ "${n_raw:-0}" -ge 1 ]; then
                record_result image select-artifact fail "$EXIT_BUILD" build image "$sel_log" "published artifact present but unusable (checksum/ambiguous/decompress); not inspecting a stale extracted tree (see $sel_log)"
                return "$EXIT_BUILD"
            fi
            log "image: no published artifact to materialise ($(tr '\n' ' ' <"$sel_log"))"
        fi
    fi
    if [ -z "$static_root" ] && [ -n "$raw" ] && [ -f "$IMAGE_DIR/extract-root.sh" ]; then
        local ex_log="$RDIR/host/image-extract-root.log"
        mkdir -p "$(dirname "$ex_log")"
        log "image: extracting checklist paths from $raw (published ${published:-none})"
        if QDISTRO_BUILD_DIR="$build_dir" bash "$IMAGE_DIR/extract-root.sh" "$raw" > "$ex_log" 2>&1; then
            static_root="$build_dir/extracted"
            extracted_fresh=1
        else
            record_result image extract-root fail "$EXIT_BUILD" build image "$ex_log" "could not extract the published raw for inspection (image/extract-root.sh needs guestfish/libguestfs; see $ex_log)"
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

    # A full run already captured this manifest for its source gate. Release
    # image-only runs capture the same configured input here before checking.
    local identity_manifest="$RDIR/release-manifest/manifest.snapshot"
    if [ -n "$static_root" ] && [ -d "$static_root" ] \
        && { [ -f "$identity_manifest" ] || [ "${QCI_RELEASE:-0}" = 1 ]; }; then
        local identity_log="$RDIR/host/image-release-identity.log"
        if [ ! -f "$identity_manifest" ]; then
            mkdir -p "$(dirname "$identity_manifest")"
            if ! cp "${QDISTRO_RELEASE_MANIFEST:-${QDISTRO_SOURCE_MANIFEST:-$QDISTRO_REPO/scripts/install/source-manifest.txt}}" "$identity_manifest"; then
                record_result image release-identity fail "$EXIT_RELEASE" release image "$identity_log" "expected release manifest unavailable"
                return "$EXIT_RELEASE"
            fi
        fi
        kv image_expected_manifest "$identity_manifest"
        kv image_expected_profile "${QDISTRO_PROFILE:-release}"
        # gate_release_manifest snapshots the configured file even when it has
        # no active pins, recording that state as blocked on development hosts.
        # Do not reinterpret that expected prerequisite gap as a malformed
        # identity and abort image boot qualification. Any active line still
        # goes through the strict verifier, including malformed/incomplete input.
        if ! grep -qEv '^[[:space:]]*(#|$)' "$identity_manifest"; then
            printf 'BLOCKED: captured release manifest has no active source pins\n' \
                > "$identity_log"
            record_blocked image release-identity "$EXIT_RELEASE" image \
                "captured release manifest is unpopulated; populate and sign it for identity qualification" "$identity_log"
        elif python3 "$IMAGE_DIR/lib/verify-release-identity.py" "$identity_manifest" \
            "$static_root" "$IMAGE_DIR/config.xml" \
            --profile "${QDISTRO_PROFILE:-release}" > "$identity_log" 2>&1; then
            record_result image release-identity pass 0 pass image "$identity_log" "image sources and build inputs match run manifest; digest=${QDISTRO_RESOLVED_DIGEST:-unavailable}"
        else
            record_result image release-identity fail "$EXIT_RELEASE" release image "$identity_log" "image does not match captured release identity"
            return "$EXIT_RELEASE"
        fi
    fi

    if [ "$no_boot" = 1 ]; then
        log "image: --no-boot set; skipping boot/install stages"
        return "$rc"
    fi

    # --- Stage B: boot-verify + install-test (needs VM + built image) -------
    # These are NOT runnable without libvirt and a built artifact. Guard each
    # and degrade to record_blocked with a precise reason.
    # Boot ONLY the artifact Stage A inspected: the raw qdistro_materialize_raw
    # produced from the checksummed xz (or the sole top-level raw when there
    # is no bundle). With --root, or with a pre-extracted fallback tree, there
    # is no provable link between the inspected tree and any bootable file,
    # so booting one would judge two different artifacts as if they were one
    # (round-4 review); that case is BLOCKED with the reason, not silently
    # booted.
    local img=""
    if [ -z "$root" ] && [ -n "$raw" ] && [ "$extracted_fresh" = 1 ]; then
        img="$raw"
    fi
    if [ -z "$img" ]; then
        local why="boot needs the published raw Stage A inspected:"
        [ -n "$root" ] && why="$why --root was given, so the inspected tree has no provable source image;"
        [ -z "$raw" ] && why="$why no bundle/*.raw.xz or single top-level .raw under $build_dir;"
        record_blocked image verify.sh "$EXIT_VM_PROVISION" image "$why run image/build-in-vm.sh and rerun without --root"
        # install-test is inert while installiso=false: no row (not blocked,
        # not skip). A blocked/skip row here is fatal under QCI_RELEASE=1.
        return "$rc"
    fi
    local have_virsh=0
    command -v virsh >/dev/null 2>&1 && "${VIRSH[@]}" list >/dev/null 2>&1 && have_virsh=1

    if [ "$have_virsh" = 0 ]; then
        local why="needs VM: libvirt session unavailable;"
        record_blocked image verify.sh "$EXIT_VM_PROVISION" image "$why run image/build-in-vm.sh on a test machine"
        return "$rc"
    fi

    # Prerequisites present: run the existing boot/install flow.
    local v_log="$RDIR/host/image-verify.log"
    log "image: boot-verify (image/verify.sh)"
    # --stick: Phase E grow/USB/persist/login/secure-boot/nested/dd matrix.
    # In `qci full` since Phase F (todo/iso/14).
    # Pass the xz when Stage A resolved one, so --stick's dd extra is the
    # same digest (iso/14 Phase E independent B2). verify.sh re-materialises
    # via the from-xz cache. Pin login/persist/grow so a caller env cannot
    # silently skip the default matrix.
    local verify_src="$img"
    [ "${QDISTRO_RESOLVED_KIND:-}" = xz ] && [ -n "${QDISTRO_RESOLVED_XZ:-}" ] && verify_src="$QDISTRO_RESOLVED_XZ"
    QDISTRO_IMAGE="$verify_src" \
      QDISTRO_VERIFY_LOGIN=1 QDISTRO_VERIFY_PERSIST=1 QDISTRO_VERIFY_GROW_GIB=64 \
      bash "$IMAGE_DIR/verify.sh" --stick > "$v_log" 2>&1
    local v_rc=$?
    if [ "$v_rc" -eq 0 ]; then
        record_result image verify.sh pass 0 pass image "$v_log" "boot-verify passed"
    else
        record_result image verify.sh fail "$EXIT_VM_BOOT" vm_boot image "$v_log" "boot-verify failed rc=$v_rc"
        [ "$rc" -eq 0 ] && rc=$EXIT_VM_BOOT
    fi

    # The tester build is the raw alone: config.xml sets installiso="false"
    # (todo/iso/13). install-test.sh is inert: no row (not skip, not blocked).
    # Do not `ls | head -1` a leftover `$build_dir/*.install.iso` — that is
    # the same shape Phase E forbade for the published raw, and would run
    # install-test against the tester xz if an old ISO sat in the build dir.
    if grep -q 'installiso="false"' "$IMAGE_DIR/config.xml" 2>/dev/null; then
        log "image: installiso=false (todo/iso/13); install-test.sh is inert until the post-v1 installable ISO returns (not a skip/blocked row)"
        return "$rc"
    fi
    local iso=""
    local iso_dir
    iso_dir="$(dirname "$img")"
    [ -n "${QDISTRO_RESOLVED_XZ:-}" ] && iso_dir="$(dirname "$QDISTRO_RESOLVED_XZ")"
    iso="$(find "$iso_dir" -maxdepth 1 -name '*.install.iso' -type f 2>/dev/null)"
    local n_iso
    n_iso="$(printf '%s\n' "$iso" | grep -c . || true)"
    if [ "${n_iso:-0}" -ne 1 ]; then
        log "image: installiso is on but $iso_dir has ${n_iso:-0} .install.iso files; install-test.sh not run"
        record_blocked image install-test.sh "$EXIT_VM_PROVISION" image "installiso is not false but no unique .install.iso next to the published artifact ($iso_dir)"
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
