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
# ---------------------------------------------------------------------------
# image_source_check <tree> -- is the auto-discovered image under <tree> built
# from the tree under test? The build stamps its source into the image as
# /etc/qdistro/release (image/lib/release-stamp.sh, written by config.sh in the
# kiwi chroot). The bundle directory itself (bundle/*.verified, *.packages,
# *.sha256) carries no source SHA, so the stamp inside the extracted tree is
# the only record. The stamp is parsed with the WRITER's grammar
# (qdistro_read_release_source in release-stamp.sh), shared with
# verify-contents.sh.
#
# Returns, setting IMAGE_SOURCE_REASON and IMAGE_SOURCE_NOTE:
#   0  a valid stamp whose qdistro SHA is HEAD or an ANCESTOR of HEAD: inspect.
#      An ancestor build is allowed but NOT proof of HEAD: IMAGE_SOURCE_NOTE
#      carries the SHA/distance into the checklist row (and the log warns).
#   1  BLOCKED: a VALID source identity that is incompatible with, or cannot be
#      tied to, the tree under test --
#        not-ancestor  proven on a complete (non-shallow) history;
#        unknown       the source commit is not in the local object db, the
#                      history is shallow, or git itself errored: the graph
#                      cannot establish ancestry (fetch/deepen, not rebuild);
#        legacy-layout the recognised pre-monorepo five-repo SOURCE schema
#                      (the 2026-09-10 sibling-layout bundle).
#   2  INVALID provenance: missing, unreadable, symlinked, duplicate or
#      malformed stamp. That is a defect of the image under test (the writer
#      is product code), so the caller still runs the checklist and FAILS.
# ---------------------------------------------------------------------------
image_source_check() {
    local tree=$1 repo rel cls kind sha state head behind err rc shallow
    local fix="rebuild the bundle from this tree with image/build-in-vm.sh, or set QDISTRO_BUILD_DIR to a build dir whose bundle was built from it"
    local fetch="fetch or deepen the history (git fetch --unshallow / git fetch origin <branch>) so ancestry can be checked, or $fix"
    IMAGE_SOURCE_REASON=""
    IMAGE_SOURCE_NOTE=""
    repo="$(project_root qdistro)"
    rel="$tree/etc/qdistro/release"
    if [ ! -f "$IMAGE_DIR/lib/release-stamp.sh" ]; then
        IMAGE_SOURCE_REASON="image source unknown: $IMAGE_DIR/lib/release-stamp.sh (the stamp grammar) is missing"
        kv image_source_relation unknown
        return 1
    fi
    # shellcheck source=../../../image/lib/release-stamp.sh
    . "$IMAGE_DIR/lib/release-stamp.sh"
    cls="$(qdistro_read_release_source "$rel")"
    read -r kind sha state _ <<<"$cls"
    # Fail closed on any classification that lacks a valid identity.
    if [ "$kind" != mono ] && [ "$kind" != legacy ]; then kind=invalid; fi
    if [ "$kind" != invalid ] && { ! [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || ! [[ "$state" =~ ^(clean|DIRTY)$ ]]; }; then
        cls="invalid $rel classified '$cls' without a 40-hex SHA and clean|DIRTY state"
        kind=invalid
    fi
    if [ "$kind" = invalid ]; then
        IMAGE_SOURCE_REASON="image provenance invalid: ${cls#invalid }"
        kv image_source_relation invalid
        return 2
    fi
    kv image_source_sha "$sha"
    kv image_source_state "$state"
    if [ "$kind" = legacy ]; then
        IMAGE_SOURCE_REASON="stale image: pre-monorepo sibling-layout build (five SOURCE lines: $QDISTRO_LEGACY_SOURCE_REPOS; qdistro $sha); it was not built from this monorepo tree; $fix"
        kv image_source_relation legacy-layout
        return 1
    fi
    if ! head="$(git -C "$repo" rev-parse --verify -q HEAD 2>&1)"; then
        IMAGE_SOURCE_REASON="image source unknown: image built from $sha, but HEAD of the tree under test ($repo) cannot be resolved (git: ${head:-no output}); $fix"
        kv image_source_relation unknown
        return 1
    fi
    kv image_tree_head "$head"
    if [ "$sha" = "$head" ]; then
        kv image_source_relation exact
        [ "$state" = clean ] || IMAGE_SOURCE_NOTE="image built from HEAD $head with a DIRTY tree"
        return 0
    fi
    shallow="$(git -C "$repo" rev-parse --is-shallow-repository 2>/dev/null || echo unknown)"
    kv image_tree_shallow "$shallow"
    err="$(git -C "$repo" cat-file -e "$sha^{commit}" 2>&1)"; rc=$?
    if [ "$rc" -ne 0 ]; then
        IMAGE_SOURCE_REASON="image source unknown: built from $sha, which is not available in the local history of the tree under test (HEAD $head; shallow=$shallow${err:+; git: $err}); cannot tell whether it is an ancestor; $fetch"
        kv image_source_relation unknown
        return 1
    fi
    err="$(git -C "$repo" merge-base --is-ancestor "$sha" "$head" 2>&1)"; rc=$?
    if [ "$rc" -eq 1 ] && [ "$shallow" = false ]; then
        IMAGE_SOURCE_REASON="stale image: built from $sha, which is not an ancestor of the tree under test (HEAD $head); $fix"
        kv image_source_relation not-ancestor
        return 1
    elif [ "$rc" -eq 1 ]; then
        local hist="in a shallow history"
        [ "$shallow" = true ] || hist="and history shallowness unknown (git rev-parse --is-shallow-repository: $shallow)"
        IMAGE_SOURCE_REASON="image source unknown: built from $sha, not reachable from HEAD $head $hist; ancestry cannot be established; $fetch"
        kv image_source_relation unknown
        return 1
    elif [ "$rc" -ne 0 ]; then
        IMAGE_SOURCE_REASON="image source unknown: git merge-base --is-ancestor $sha $head failed rc=$rc (${err:-no output}); ancestry cannot be established; $fetch"
        kv image_source_relation unknown
        return 1
    fi
    behind="$(git -C "$repo" rev-list --count "$sha..$head" 2>/dev/null || echo '?')"
    kv image_source_relation ancestor
    kv image_source_behind "$behind"
    IMAGE_SOURCE_NOTE="image built from ancestor $sha ($state), $behind commit(s) behind HEAD $head; not proof of HEAD"
    log "image: WARNING $IMAGE_SOURCE_NOTE"
    return 0
}

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
    # An auto-discovered tree must come from this tree's history, or its
    # checklist verdict is about some other source (the 2026-09-10 sibling-
    # layout bundle "failed" verify-contents on the monorepo). An explicit
    # --root is the caller's deliberate choice and is inspected as given.
    local source_block="" source_invalid="" source_note=""
    if [ -z "$root" ] && [ -n "$static_root" ] && [ -d "$static_root" ]; then
        image_source_check "$static_root"
        case $? in
            0) source_note="$IMAGE_SOURCE_NOTE" ;;
            1) source_block="$IMAGE_SOURCE_REASON"
               log "image: $source_block"
               record_blocked image verify-contents "$EXIT_BUILD" image "$source_block" ;;
            *) source_invalid="$IMAGE_SOURCE_REASON"
               log "image: $source_invalid" ;;
        esac
    fi
    if [ -n "$source_block" ]; then
        :   # blocked above; do not judge (or boot) an unrelated build
    elif [ -n "$root" ] || { [ -n "$static_root" ] && [ -d "$static_root" ]; }; then
        local sc_log="$RDIR/host/image-verify-contents.log"
        mkdir -p "$(dirname "$sc_log")"
        log "image: static content checklist against ${static_root:-<unset>}"
        bash "$checker" "$static_root" > "$sc_log" 2>&1
        local sc_rc=$?
        local sc_extra=""
        [ -n "$source_note" ] && sc_extra="; $source_note"
        [ -n "$source_invalid" ] && sc_extra="; $source_invalid"
        if [ "$sc_rc" -eq 0 ] && [ -z "$source_invalid" ]; then
            record_result image verify-contents pass 0 pass image "$sc_log" "static checklist passed ($static_root)$sc_extra"
        else
            # Invalid provenance fails even if every checklist row passed:
            # an image that cannot say what went in is not a tester image.
            local verdict="static checklist failed rc=$sc_rc"
            [ "$sc_rc" -eq 0 ] && verdict="static checklist passed but provenance is invalid"
            record_result image verify-contents fail "$EXIT_BUILD" build image "$sc_log" "$verdict ($static_root)$sc_extra"
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
    if [ -z "$source_block" ] && [ -n "$static_root" ] && [ -d "$static_root" ] \
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
        # Record the OMITTED stages explicitly. Returning silently left no trace
        # in results.tsv, and release completeness is a scan over results.tsv —
        # so a static-only developer run produced a row set indistinguishable
        # from one that actually booted and installed. Absence of a failure row
        # is not evidence a required stage ran. QCI_RELEASE=1 escalates these
        # blocked rows, so --no-boot can no longer qualify a boot-required run.
        # EXIT_VM_PROVISION, matching the other blocked rows for these SAME two
        # stages (verify.sh / install-test.sh below): the VM-backed stage did not
        # run. EXIT_USAGE (2) is not in the exit-class table at all and would
        # render as the bogus class `unknown(2)`. This records rows only; the
        # gate's return value is unchanged, so gate_full's vm-provision infra
        # cascade is not triggered by a --no-boot developer run.
        record_blocked image boot-verify "$EXIT_VM_PROVISION" image \
            "stage omitted: --no-boot (static-only developer mode; no boot evidence for this run)"
        record_blocked image install-test "$EXIT_VM_PROVISION" image \
            "stage omitted: --no-boot (static-only developer mode; no install evidence for this run)"
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
    if [ -z "$root" ] && [ -z "$source_block" ] && [ -n "$raw" ] && [ "$extracted_fresh" = 1 ]; then
        img="$raw"
    fi
    if [ -z "$img" ]; then
        local why="boot needs the published raw Stage A inspected:"
        [ -n "$source_block" ] && why="$why the published image was not built from this tree (see the verify-contents row);"
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
