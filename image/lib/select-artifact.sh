#!/bin/bash
# select-artifact.sh — pick the disk image verify.sh / the image gate boot.
# Sourced. Never exits: prints errors on stderr and returns non-zero so a
# bats test can source it. Callers that want a hard stop (`verify.sh`)
# die on a non-zero return.
#
# Why this file exists (todo/iso/14 Phase E item 5): a `find | head -1`
# among a stale qcow2 and a fresh raw booted an arbitrary artifact. The
# published tester file is the checksummed `.raw.xz`; the gate must
# judge that file (digest, xz -t, decompress) and boot the bytes it
# produced, not a sibling `.raw` that happened to sit next to it.
#
# qdistro_resolve_image
#   Reads QDISTRO_IMAGE, QDISTRO_IMAGE_SHA256, QDISTRO_BUILD_DIR.
#   Sets:
#     QDISTRO_RESOLVED_PATH   the file the caller named (raw, qcow2, or xz)
#     QDISTRO_RESOLVED_KIND   raw | qcow2 | xz
#     QDISTRO_RESOLVED_DIGEST sha256 of the xz, if kind=xz
#     QDISTRO_RESOLVED_XZ     path of the xz, if kind=xz
#
# qdistro_materialize_raw
#   If kind=xz: sha256sum -c, xz -t, decompress to
#   $BUILD_DIR/from-xz-<digest12>.raw (reused when size matches xz -l).
#   Sets QDISTRO_RESOLVED_DISK to a bootable raw/qcow2 path.
#
# A digest may be passed as QDISTRO_IMAGE (64 hex) or QDISTRO_IMAGE_SHA256
# (then QDISTRO_IMAGE is the file, and the digest must match).

qdistro_resolve_image() {
    local build_dir img digest
    build_dir="${QDISTRO_BUILD_DIR:-/var/tmp/qdistro-build}"
    img="${QDISTRO_IMAGE:-}"
    digest="${QDISTRO_IMAGE_SHA256:-}"
    QDISTRO_RESOLVED_PATH=""
    QDISTRO_RESOLVED_KIND=""
    QDISTRO_RESOLVED_DIGEST=""
    QDISTRO_RESOLVED_XZ=""
    QDISTRO_RESOLVED_DISK=""

    if [ -n "$digest" ]; then
        case "$digest" in
            *[!0-9a-fA-F]*|"") echo "select-artifact: QDISTRO_IMAGE_SHA256 is not a hex digest: $digest" >&2; return 2 ;;
        esac
        digest=$(printf '%s' "$digest" | tr 'A-F' 'a-f')
        [ "${#digest}" -eq 64 ] || { echo "select-artifact: QDISTRO_IMAGE_SHA256 must be 64 hex chars, got ${#digest}" >&2; return 2; }
    fi

    if [ -n "$img" ]; then
        case "$img" in
            *[!0-9a-fA-F]*)
                # a path (may still be relative)
                ;;
            *)
                if [ "${#img}" -eq 64 ]; then
                    # QDISTRO_IMAGE is itself the digest
                    if [ -n "$digest" ] && [ "$digest" != "$(printf '%s' "$img" | tr 'A-F' 'a-f')" ]; then
                        echo "select-artifact: QDISTRO_IMAGE digest $img disagrees with QDISTRO_IMAGE_SHA256 $digest" >&2
                        return 2
                    fi
                    digest=$(printf '%s' "$img" | tr 'A-F' 'a-f')
                    img=""
                fi
                ;;
        esac
    fi

    if [ -n "$digest" ] && [ -z "$img" ]; then
        img="$(qdistro_find_by_digest "$digest" "$build_dir")" || return $?
    fi

    if [ -z "$img" ]; then
        img="$(qdistro_discover_image "$build_dir")" || return $?
    fi

    [ -f "$img" ] || { echo "select-artifact: not a file: $img" >&2; return 2; }
    # Canonicalise so later cmp/stat agree; refuse characters that would
    # split a generated domain XML or a guestfish stream.
    img="$(realpath -e -- "$img")" || return 2
    case "$img" in
        *[[:space:]\"\'\\]*) echo "select-artifact: refusing path with whitespace or quotes: $img" >&2; return 2 ;;
    esac

    QDISTRO_RESOLVED_PATH="$img"
    case "$img" in
        *.raw.xz)
            QDISTRO_RESOLVED_KIND=xz
            QDISTRO_RESOLVED_XZ="$img"
            qdistro_verify_xz_checksum "$img" "$digest" || return $?
            ;;
        *.raw)
            QDISTRO_RESOLVED_KIND=raw
            QDISTRO_RESOLVED_DISK="$img"
            if [ -n "$digest" ]; then
                echo "select-artifact: QDISTRO_IMAGE_SHA256 is set but $img is a raw, not the checksummed xz" >&2
                return 2
            fi
            ;;
        *.qcow2)
            QDISTRO_RESOLVED_KIND=qcow2
            QDISTRO_RESOLVED_DISK="$img"
            if [ -n "$digest" ]; then
                echo "select-artifact: QDISTRO_IMAGE_SHA256 is set but $img is a qcow2, not the checksummed xz" >&2
                return 2
            fi
            ;;
        *)
            echo "select-artifact: unknown image format: $img (want .raw, .raw.xz or .qcow2)" >&2
            return 2
            ;;
    esac
    return 0
}

# Discover the published artifact under $build_dir. Preference, in order:
#   1. exactly one bundle/*.raw.xz  — the tester download
#   2. exactly one top-level *.raw  — a just-built unpackaged raw
# Zero of both, or more than one of the chosen kind, is an error. Never
# "the first find hit".
qdistro_discover_image() {
    local build_dir="$1"
    local -a xzs raws
    mapfile -t xzs < <(find "$build_dir/bundle" -maxdepth 1 -name '*.raw.xz' -type f 2>/dev/null | sort)
    mapfile -t raws < <(find "$build_dir" -maxdepth 1 -name '*.raw' -type f 2>/dev/null | sort)
    if [ "${#xzs[@]}" -eq 1 ]; then
        printf '%s\n' "${xzs[0]}"
        return 0
    fi
    if [ "${#xzs[@]}" -gt 1 ]; then
        echo "select-artifact: ${#xzs[@]} .raw.xz under $build_dir/bundle (${xzs[*]}); set QDISTRO_IMAGE or QDISTRO_IMAGE_SHA256" >&2
        return 2
    fi
    if [ "${#raws[@]}" -eq 1 ]; then
        printf '%s\n' "${raws[0]}"
        return 0
    fi
    if [ "${#raws[@]}" -gt 1 ]; then
        echo "select-artifact: ${#raws[@]} .raw under $build_dir (${raws[*]}); set QDISTRO_IMAGE" >&2
        return 2
    fi
    echo "select-artifact: no image in $build_dir (no bundle/*.raw.xz, no *.raw); run build-in-vm.sh first" >&2
    return 2
}

# Find the unique *.sha256 under $dir (maxdepth 2) whose first field is
# $digest, and print the artifact it names (sibling of the checksum file).
qdistro_find_by_digest() {
    local digest="$1" dir="$2" f rec name hits=()
    [ -d "$dir" ] || { echo "select-artifact: build dir does not exist: $dir" >&2; return 2; }
    while IFS= read -r -d '' f; do
        rec=$(awk 'NR==1 {print tolower($1)}' "$f")
        [ "$rec" = "$digest" ] || continue
        hits+=("$f")
    done < <(find "$dir" -maxdepth 2 -name '*.sha256' -type f -print0 2>/dev/null)
    case "${#hits[@]}" in
        1)
            name="$(awk 'NR==1 { sub(/^[0-9a-fA-F]+ [ *]/, ""); print }' "${hits[0]}")"
            [ -n "$name" ] || { echo "select-artifact: ${hits[0]} has no filename field" >&2; return 2; }
            printf '%s\n' "$(dirname "${hits[0]}")/$name"
            return 0
            ;;
        0)
            echo "select-artifact: no .sha256 under $dir matches digest $digest" >&2
            return 2
            ;;
        *)
            echo "select-artifact: ${#hits[@]} .sha256 files match digest $digest: ${hits[*]}" >&2
            return 2
            ;;
    esac
}

# Verify the xz's sibling (or explicit) checksum file. Sets
# QDISTRO_RESOLVED_DIGEST to the hex in that file.
qdistro_verify_xz_checksum() {
    local xz="$1" want="$2"
    local sum name rec sum_lines
    if [ -n "${QDISTRO_IMAGE_SHA256_FILE:-}" ]; then
        sum="$QDISTRO_IMAGE_SHA256_FILE"
    else
        sum="$xz.sha256"
    fi
    [ -s "$sum" ] || { echo "select-artifact: checksum file missing: $sum" >&2; return 2; }
    sum_lines="$(grep -c . "$sum")"
    [ "$sum_lines" = 1 ] || { echo "select-artifact: $sum has $sum_lines records, want 1" >&2; return 2; }
    rec="$(awk 'NR==1 {print tolower($1)}' "$sum")"
    name="$(awk 'NR==1 { sub(/^[0-9a-fA-F]+ [ *]/, ""); print }' "$sum")"
    [ "$name" = "$(basename "$xz")" ] || {
        echo "select-artifact: $sum names '$name', not $(basename "$xz")" >&2
        return 2
    }
    if [ -n "$want" ] && [ "$rec" != "$want" ]; then
        echo "select-artifact: $sum digest $rec disagrees with requested $want" >&2
        return 2
    fi
    (cd "$(dirname "$xz")" && sha256sum -c "$(basename "$sum")") >&2 || {
        echo "select-artifact: sha256 mismatch for $xz" >&2
        return 2
    }
    QDISTRO_RESOLVED_DIGEST="$rec"
    return 0
}

# Turn a resolved xz into a raw the rest of the pipeline can boot / extract.
# Reuse only after comparing every byte with a fresh decompression.
qdistro_materialize_raw() {
    local build_dir dest tmp unc digest
    build_dir="${QDISTRO_BUILD_DIR:-/var/tmp/qdistro-build}"
    case "${QDISTRO_RESOLVED_KIND:-}" in
        raw|qcow2)
            QDISTRO_RESOLVED_DISK="${QDISTRO_RESOLVED_DISK:-$QDISTRO_RESOLVED_PATH}"
            return 0
            ;;
        xz) ;;
        *) echo "select-artifact: qdistro_materialize_raw before qdistro_resolve_image" >&2; return 2 ;;
    esac
    [ -n "$QDISTRO_RESOLVED_DIGEST" ] || { echo "select-artifact: xz has no digest" >&2; return 2; }
    xz -t -T0 "$QDISTRO_RESOLVED_XZ" || { echo "select-artifact: xz -t failed: $QDISTRO_RESOLVED_XZ" >&2; return 2; }
    unc="$(xz -l --robot "$QDISTRO_RESOLVED_XZ" | awk '$1 == "totals" { print $5 }')"
    [ -n "$unc" ] && [ "$unc" -gt 0 ] 2>/dev/null || { echo "select-artifact: xz -l did not report uncompressed size" >&2; return 2; }
    # Not next to the kiwi *.raw: extract-root.sh and a human `ls *.raw`
    # still see exactly one top-level raw. The published bytes live here.
    mkdir -p "$build_dir/published"
    dest="$build_dir/published/from-xz-${QDISTRO_RESOLVED_DIGEST}.raw"
    tmp="$(mktemp "$dest.partial.XXXXXXXX")" || return 2
    echo "select-artifact: decompressing $QDISTRO_RESOLVED_XZ for full raw verification ($unc bytes)" >&2
    if ! xz -dc -T0 "$QDISTRO_RESOLVED_XZ" > "$tmp"; then
        rm -f "$tmp"
        echo "select-artifact: xz -dc failed" >&2
        return 2
    fi
    digest="$(sha256sum "$QDISTRO_RESOLVED_XZ")" || { rm -f "$tmp"; return 2; }
    if [ "${digest%% *}" != "$QDISTRO_RESOLVED_DIGEST" ] || [ "$(stat -c %s "$tmp")" != "$unc" ]; then
        echo "select-artifact: artifact changed or decompressed size disagrees" >&2
        rm -f "$tmp"
        return 2
    fi
    # Unique temporary files and atomic rename keep interrupted/concurrent
    # writers from publishing partial bytes. Never trust a cached sidecar hash.
    if [ ! -L "$dest" ] && [ -f "$dest" ] && cmp -s "$tmp" "$dest"; then
        rm -f "$tmp"
        echo "select-artifact: verified all bytes of $dest" >&2
    elif ! mv -f "$tmp" "$dest"; then
        rm -f "$tmp"
        return 2
    fi
    QDISTRO_RESOLVED_DISK="$dest"
    return 0
}

# cmp the first and last MiB of $1 against $2 (the published raw). Used
# after xzcat | dd to prove the write landed.
qdistro_cmp_ends() {
    local a="$1" b="$2" bs=1048576 sa sb
    sa="$(stat -c %s "$a")"
    sb="$(stat -c %s "$b")"
    [ "$sa" = "$sb" ] || { echo "select-artifact: size $a ($sa) != $b ($sb)" >&2; return 1; }
    [ "$sa" -ge "$bs" ] || { echo "select-artifact: image shorter than 1 MiB" >&2; return 1; }
    cmp -n "$bs" "$a" "$b" || { echo "select-artifact: first MiB differs: $a vs $b" >&2; return 1; }
    # last MiB: skip (size-1MiB) bytes. dd+cmp so we do not read the middle.
    cmp <(dd if="$a" bs="$bs" skip=$((sa / bs - 1)) count=1 status=none) \
        <(dd if="$b" bs="$bs" skip=$((sb / bs - 1)) count=1 status=none) \
        || { echo "select-artifact: last MiB differs: $a vs $b" >&2; return 1; }
    return 0
}

# Stream xzcat | dd onto $dest (a new file), then cmp ends against
# QDISTRO_RESOLVED_DISK (the materialised raw).
qdistro_dd_from_xz() {
    local dest="$1"
    [ -n "${QDISTRO_RESOLVED_XZ:-}" ] || { echo "select-artifact: qdistro_dd_from_xz needs a resolved xz" >&2; return 2; }
    [ -n "${QDISTRO_RESOLVED_DISK:-}" ] || { echo "select-artifact: qdistro_dd_from_xz needs a materialised raw" >&2; return 2; }
    [ -e "$dest" ] && { echo "select-artifact: refusing to overwrite $dest" >&2; return 2; }
    mkdir -p "$(dirname "$dest")"
    echo "select-artifact: xzcat | dd -> $dest" >&2
    xzcat -- "$QDISTRO_RESOLVED_XZ" | dd of="$dest" bs=4M conv=sparse,fsync status=none \
        || { echo "select-artifact: xzcat | dd failed" >&2; rm -f "$dest"; return 2; }
    qdistro_cmp_ends "$dest" "$QDISTRO_RESOLVED_DISK" || { rm -f "$dest"; return 1; }
    return 0
}
