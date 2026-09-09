#!/usr/bin/env bash
# qci module: cleanup gate
# Extracted from bin/qci. SOURCED by bin/qci into the single CI-runner
# process; it is not executed standalone. See ci/AGENTS.md for the module map.
# shellcheck shell=bash

# Print TYPE, DEVICE, TARGET and SOURCE for one libvirt storage view.
cleanup_storage_rows() {
    local name=$1 view=$2 output
    local -a view_arg=()
    [ "$view" = inactive ] && view_arg=(--inactive)
    output=$("${VIRSH[@]}" domblklist "$name" --details \
        "${view_arg[@]}" 2>/dev/null) || return 1
    printf '%s\n' "$output" | awk '
        $1 == "Type" || $1 ~ /^-+$/ || NF == 0 { next }
        NF != 4 { print "MALFORMED\tMALFORMED\tMALFORMED\tMALFORMED"; next }
        { print $1 "\t" $2 "\t" $3 "\t" $4 }
    '
}

cleanup_is_disposable_disk() {
    local path=$1 img_dir base
    img_dir="${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}"
    base=$(basename -- "$path")
    case "$base" in qci-*.qcow2) ;; *) return 1 ;; esac
    [ "$path" = "$img_dir/$base" ] && [ -f "$path" ] && [ ! -L "$path" ]
}

# Capture all defined-domain ownership into stable, sorted files. A caller may
# only delete storage when this audit succeeds completely.
cleanup_capture_inventory() {
    local inventory=$1 names_file=$2 log_path=$3 context=$4
    local defined_names owner state view rows type device target source canonical
    local capture_rc=0
    : > "$inventory"
    : > "$names_file"
    if ! defined_names=$("${VIRSH[@]}" list --all --name 2>/dev/null); then
        echo "$context: could not list defined domains" >> "$log_path"
        return 1
    fi
    printf '%s\n' "$defined_names" | sed '/^$/d' | LC_ALL=C sort -u > "$names_file"
    while IFS= read -r owner; do
        [ -n "$owner" ] || continue
        if ! state=$("${VIRSH[@]}" domstate "$owner" 2>/dev/null); then
            capture_rc=1
            echo "$context: could not read state for $owner" >> "$log_path"
            continue
        fi
        for view in live inactive; do
            if ! rows=$(cleanup_storage_rows "$owner" "$view"); then
                capture_rc=1
                echo "$context: could not inspect $view storage for $owner" >> "$log_path"
                continue
            fi
            if [ -z "$rows" ]; then
                capture_rc=1
                echo "$context: $owner has no inspectable $view storage" >> "$log_path"
                continue
            fi
            while IFS=$'\t' read -r type device target source; do
                [ -n "$type" ] || continue
                if [ "$type" != file ]; then
                    capture_rc=1
                    echo "$context: $owner $view has non-file $type/$device source=$source" >> "$log_path"
                    continue
                fi
                case "$source" in
                    /*) ;;
                    *) capture_rc=1
                       echo "$context: $owner $view has unresolved source=$source" >> "$log_path"
                       continue ;;
                esac
                if ! canonical=$(readlink -e -- "$source" 2>/dev/null); then
                    capture_rc=1
                    echo "$context: $owner $view path cannot be canonicalized: $source" >> "$log_path"
                    continue
                fi
                printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
                    "$owner" "$view" "$type" "$device" "$target" \
                    "$source" "$canonical" >> "$inventory"
            done <<< "$rows"
        done
        case "$state" in
            "shut off"|inactive) ;;
            *)
                while IFS= read -r canonical; do
                    [ -n "$canonical" ] || continue
                    if ! awk -F '\t' -v n="$owner" -v p="$canonical" \
                            '$1==n && $2=="inactive" && $7==p {found=1} END {exit !found}' \
                            "$inventory"; then
                        capture_rc=1
                        echo "$context: $owner has live-only attachment $canonical" >> "$log_path"
                    fi
                done < <(awk -F '\t' -v n="$owner" \
                    '$1==n && $2=="live" {print $7}' "$inventory" | sort -u)
                ;;
        esac
    done < "$names_file"
    LC_ALL=C sort -o "$inventory" "$inventory"
    return "$capture_rc"
}

# Repeat the complete audit at the deletion boundary. The expected snapshot is
# updated only for domains this cleanup has successfully undefined.
cleanup_refresh_allows_unlink() {
    local candidate=$1 expected_inventory=$2 expected_names=$3
    local refresh_inventory=$4 refresh_names=$5 log_path=$6 context=$7 owners
    if ! cleanup_capture_inventory "$refresh_inventory" "$refresh_names" \
            "$log_path" "$context ownership audit incomplete"; then
        echo "keep $candidate ($context ownership audit incomplete)" >> "$log_path"
        return 1
    fi
    owners=$(awk -F '\t' -v p="$candidate" \
        '$7==p && !seen[$1]++ {print $1}' "$refresh_inventory" | paste -sd, -)
    if [ -n "$owners" ]; then
        echo "keep $candidate ($context ownership changed; attached to $owners)" >> "$log_path"
        return 1
    fi
    if ! cmp -s "$expected_names" "$refresh_names" \
            || ! cmp -s "$expected_inventory" "$refresh_inventory"; then
        echo "keep $candidate ($context ownership inventory changed)" >> "$log_path"
        return 1
    fi
    return 0
}

gate_cleanup() {
    qci_assert_run_dir || return $?
    local dry=0 age_hours=24 rc=$EXIT_OK log_path="$RDIR/host/cleanup.log"
    while [ $# -gt 0 ]; do
        case "$1" in
            --dry-run) dry=1 ;;
            --age-hours) shift; age_hours=${1:-24} ;;
            *) record_blocked cleanup "$1" "$EXIT_USAGE" args "unknown cleanup flag"; return "$EXIT_USAGE" ;;
        esac
        shift
    done
    : > "$log_path"

    local now cutoff inventory names_file defined_names audit_complete=1
    local refresh_inventory refresh_names
    now=$(date +%s)
    cutoff=$((now - age_hours * 3600))
    inventory="$RDIR/host/cleanup-storage-inventory.tsv"
    names_file="$RDIR/host/cleanup-storage-domains.txt"
    refresh_inventory="$RDIR/host/cleanup-storage-refresh.tsv"
    refresh_names="$RDIR/host/cleanup-storage-refresh-domains.txt"

    if ! cleanup_capture_inventory "$inventory" "$names_file" "$log_path" \
            "storage audit incomplete"; then
        audit_complete=0
    fi
    defined_names=$(cat "$names_file")

    local name disk mtime candidate candidate_mtime ref_state unsafe other raw_path
    local -a domain_disks=()
    while IFS= read -r name; do
        [ -n "$name" ] || continue
        case "$name" in qci-*) ;; *) continue ;; esac
        if is_protected_vm "$name"; then
            echo "skip protected $name" >> "$log_path"
            continue
        fi
        if [ "$audit_complete" != 1 ]; then
            echo "keep $name (global storage ownership audit incomplete)" >> "$log_path"
            continue
        fi

        domain_disks=()
        unsafe=""
        disk=""
        while IFS=$'\t' read -r type device target raw_path canonical; do
            [ -n "$type" ] || continue
            if [ "$type" != file ] || [ "$device" != disk ] \
                    || [ "$raw_path" != "$canonical" ] \
                    || ! cleanup_is_disposable_disk "$canonical"; then
                unsafe="$type/$device $target source=$raw_path (not a canonical disposable overlay)"
                break
            fi
            domain_disks+=("$canonical")
            [ -n "$disk" ] || disk="$canonical"
        done < <(awk -F '\t' -v n="$name" '$1==n && $2=="inactive" {print $3 "\t" $4 "\t" $5 "\t" $6 "\t" $7}' "$inventory")
        if [ -n "$unsafe" ] || [ -z "$disk" ]; then
            echo "keep $name storage=${unsafe:-no inactive disposable disk}" >> "$log_path"
            continue
        fi
        mtime=$(stat -c %Y "$disk" 2>/dev/null || echo 0)
        if [ "$mtime" = 0 ] || [ "$mtime" -ge "$cutoff" ]; then
            echo "keep recent/unstattable $name mtime=$mtime" >> "$log_path"
            continue
        fi

        for candidate in "${domain_disks[@]}"; do
            candidate_mtime=$(stat -c %Y "$candidate" 2>/dev/null || echo 0)
            if [ "$candidate_mtime" = 0 ] || [ "$candidate_mtime" -ge "$cutoff" ]; then
                unsafe="$candidate (recent or could not be statted)"
                break
            fi
            other=$(awk -F '\t' -v n="$name" -v p="$candidate" \
                '$7==p && $1!=n && !seen[$1]++ {print $1}' "$inventory" \
                | paste -sd, -)
            if [ -n "$other" ]; then
                unsafe="$candidate (directly owned by other domain: $other)"
                break
            fi
            ref_state=$(backing_referrer_state "$candidate")
            if [ "$ref_state" != clear ]; then
                unsafe="$candidate (backing-referrer audit: $ref_state)"
                break
            fi
        done
        if [ -n "$unsafe" ]; then
            echo "keep $name disk=$unsafe" >> "$log_path"
            continue
        fi
        if [ "$dry" = 1 ]; then
            echo "would destroy $name disk=$disk" >> "$log_path"
            continue
        fi

        local destroy_rc=0 stopped_state=""
        "${VIRSH[@]}" destroy "$name" >> "$log_path" 2>&1 || destroy_rc=$?
        stopped_state=$("${VIRSH[@]}" domstate "$name" 2>> "$log_path") || true
        case "$stopped_state" in
            "shut off"|inactive)
                [ "$destroy_rc" -eq 0 ] \
                    || echo "destroy $name returned $destroy_rc; verified state=$stopped_state" >> "$log_path"
                ;;
            *)
                echo "FAIL destroy $name rc=$destroy_rc state=${stopped_state:-unknown}; preserving domain and disks" >> "$log_path"
                rc=$EXIT_RUNNER
                continue
                ;;
        esac

        if "${VIRSH[@]}" undefine "$name" --managed-save --nvram >> "$log_path" 2>&1; then
            :
        elif "${VIRSH[@]}" undefine "$name" --managed-save >> "$log_path" 2>&1; then
            echo "undefine $name succeeded with BIOS-compatible fallback" >> "$log_path"
        else
            echo "FAIL undefine $name" >> "$log_path"
            rc=$EXIT_RUNNER
            continue
        fi
        awk -F '\t' -v n="$name" '$1!=n' "$inventory" > "$inventory.next"
        grep -Fxv -- "$name" "$names_file" > "$names_file.next" || true
        mv -- "$inventory.next" "$inventory"
        mv -- "$names_file.next" "$names_file"
        for candidate in "${domain_disks[@]}"; do
            [ -f "$candidate" ] || continue
            if ! cleanup_refresh_allows_unlink "$candidate" "$inventory" \
                    "$names_file" "$refresh_inventory" "$refresh_names" \
                    "$log_path" "post-undefine"; then
                audit_complete=0
                rc=$EXIT_RUNNER
                continue
            fi
            # Run this after the ownership refresh: a concurrent worker can
            # create a qcow2 child without changing any libvirt attachment.
            ref_state=$(backing_referrer_state "$candidate")
            if [ "$ref_state" != clear ]; then
                echo "keep undefined-domain overlay $candidate (final backing-referrer audit: $ref_state)" >> "$log_path"
                audit_complete=0
                rc=$EXIT_RUNNER
            elif safe_rm_overlay "$candidate"; then
                echo "removed domain overlay $candidate" >> "$log_path"
            else
                echo "FAIL remove domain overlay $candidate" >> "$log_path"
                rc=$EXIT_RUNNER
            fi
        done
    done <<< "$defined_names"

    local orphans=0 f f_canonical obase mt attached_owner
    for f in "${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}"/qci-*.qcow2; do
        [ -f "$f" ] || continue
        obase=$(basename -- "$f")
        if [ "$audit_complete" != 1 ]; then
            echo "keep $obase (global storage ownership audit incomplete)" >> "$log_path"
            continue
        fi
        if ! f_canonical=$(readlink -e -- "$f" 2>/dev/null); then
            echo "keep $obase (path cannot be canonicalized)" >> "$log_path"
            continue
        fi
        attached_owner=$(awk -F '\t' -v p="$f_canonical" \
            '$7==p && !seen[$1]++ {print $1}' "$inventory" | paste -sd, -)
        if [ -n "$attached_owner" ]; then
            echo "keep $obase (attached to defined domain $attached_owner)" >> "$log_path"
            continue
        fi
        mt=$(stat -c %Y "$f" 2>/dev/null || echo 0)
        if [ "$mt" = 0 ] || [ "$mt" -ge "$cutoff" ]; then
            echo "keep recent/unstattable orphan $obase mtime=$mt" >> "$log_path"
            continue
        fi
        # An already-referenced candidate is safely ineligible and does not
        # indicate a race in this cleanup attempt.
        ref_state=$(backing_referrer_state "$f_canonical")
        if [ "$ref_state" != clear ]; then
            echo "keep orphan $obase (backing-referrer audit: $ref_state)" >> "$log_path"
            continue
        fi
        if [ "$dry" = 1 ]; then
            echo "would remove orphan overlay $f" >> "$log_path"
            orphans=$((orphans + 1))
        elif ! cleanup_refresh_allows_unlink "$f_canonical" "$inventory" \
                "$names_file" "$refresh_inventory" "$refresh_names" \
                "$log_path" "orphan"; then
            audit_complete=0
            rc=$EXIT_RUNNER
        else
            # Ownership can remain identical while a new qcow2 child appears.
            # Make the backing-chain audit the final check before unlink.
            ref_state=$(backing_referrer_state "$f_canonical")
            if [ "$ref_state" != clear ]; then
                echo "keep orphan $obase (final backing-referrer audit: $ref_state)" >> "$log_path"
                audit_complete=0
                rc=$EXIT_RUNNER
            elif safe_rm_overlay "$f"; then
                echo "removed orphan overlay $f" >> "$log_path"
                log "reclaimed orphan overlay $f"
                orphans=$((orphans + 1))
            else
                echo "FAIL remove orphan overlay $f" >> "$log_path"
                rc=$EXIT_RUNNER
            fi
        fi
    done

    if [ "$rc" -eq 0 ]; then
        record_result cleanup qci-vms pass "$rc" "$(exit_class_name "$rc")" vm "$log_path" "age_hours=$age_hours dry_run=$dry orphans=$orphans"
    else
        record_result cleanup qci-vms fail "$rc" "$(exit_class_name "$rc")" vm "$log_path" "age_hours=$age_hours dry_run=$dry orphans=$orphans"
    fi
    return "$rc"
}
