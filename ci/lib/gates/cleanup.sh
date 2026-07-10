#!/usr/bin/env bash
# qci module: cleanup gate
# Extracted verbatim from bin/qci. SOURCED by bin/qci into the single
# CI-runner process (shared RDIR/CREATED_VMS/golden state/traps); it is
# NOT executed standalone. See ci/AGENTS.md for the module map.
# shellcheck shell=bash

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
    local now cutoff name disk mtime
    now=$(date +%s)
    cutoff=$((now - age_hours * 3600))
    while IFS= read -r name; do
        [ -n "$name" ] || continue
        case "$name" in qci-*) ;; *) continue ;; esac
        if is_protected_vm "$name"; then
            echo "skip protected $name" >> "$log_path"
            continue
        fi
        disk=$(vm_disk_path "$name" || true)
        if [ -z "$disk" ] || [ ! -f "$disk" ]; then
            echo "keep $name (disk path unknown)" >> "$log_path"
            continue
        fi
        mtime=$(stat -c %Y "$disk" 2>/dev/null || echo 0)
        if [ "$mtime" = 0 ]; then
            echo "keep $name (could not stat disk $disk)" >> "$log_path"
            continue
        fi
        if [ "$mtime" -ge "$cutoff" ]; then
            echo "keep recent $name mtime=$mtime" >> "$log_path"
            continue
        fi
        if [ "$dry" = 1 ]; then
            echo "would destroy $name disk=$disk" >> "$log_path"
        else
            "${VIRSH[@]}" destroy "$name" >/dev/null 2>&1 || true
            # --managed-save: hibernated failed VMs carry a managedsave image
            # that undefine must be told to remove.
            "${VIRSH[@]}" undefine "$name" --remove-all-storage --managed-save >> "$log_path" 2>&1 || {
                echo "FAIL undefine $name" >> "$log_path"
                rc=$EXIT_RUNNER
            }
        fi
    done < <("${VIRSH[@]}" list --all --name 2>/dev/null)

    # Orphan-overlay sweep: the domain loop above only sees DEFINED domains, so
    # an overlay whose domain is already gone (the release_vm leak, or a SIGKILL'd
    # run) is invisible there and never reclaimed. Scan the images dir directly
    # and remove stale qci-*.qcow2 files that have no matching defined domain.
    local img_dir defined_names orphans=0 f obase mt ref_state
    img_dir="${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}"
    defined_names=$("${VIRSH[@]}" list --all --name 2>/dev/null)
    for f in "$img_dir"/qci-*.qcow2; do
        [ -f "$f" ] || continue
        obase=$(basename -- "$f")
        # Never touch backing/daily images even if name-prefixed oddly.
        case "$obase" in qdistro-daily*) continue ;; qci-*) ;; *) continue ;; esac
        # Skip if a defined domain owns this overlay (basename minus .qcow2).
        if printf '%s\n' "$defined_names" | grep -Fxq "${obase%.qcow2}"; then
            echo "keep $obase (defined domain exists)" >> "$log_path"
            continue
        fi
        mt=$(stat -c %Y "$f" 2>/dev/null || echo 0)
        if [ "$mt" = 0 ]; then
            echo "keep orphan $obase (could not stat)" >> "$log_path"
            continue
        fi
        if [ "$mt" -ge "$cutoff" ]; then
            echo "keep recent orphan $obase mtime=$mt" >> "$log_path"
            continue
        fi
        # Undefined qci goldens are legitimate backing files for worker
        # overlays. Never remove an old candidate until the same fail-closed
        # backing-chain audit used by end-of-run golden cleanup proves it clear.
        ref_state=$(backing_referrer_state "$f")
        if [ "$ref_state" != clear ]; then
            echo "keep orphan $obase (backing-referrer audit: $ref_state)" >> "$log_path"
            continue
        fi
        if [ "$dry" = 1 ]; then
            echo "would remove orphan overlay $f" >> "$log_path"
            orphans=$((orphans + 1))
        else
            if safe_rm_overlay "$f"; then
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
