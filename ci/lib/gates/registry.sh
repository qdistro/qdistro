#!/usr/bin/env bash
# qci module: registry-check gate
# Extracted verbatim from bin/qci. SOURCED by bin/qci into the single
# CI-runner process (shared RDIR/CREATED_VMS/golden state/traps); it is
# NOT executed standalone. See ci/AGENTS.md for the module map.
# shellcheck shell=bash

# ---------------------------------------------------------------------------
# Registry consistency check (pilot, advisory). Verifies each listed path
# exists and each gate name is one qci knows. Never gates qci full.
# ---------------------------------------------------------------------------
gate_registry_check() {
    qci_assert_run_dir || return $?
    local log_path="$RDIR/host/registry-check.log"
    mkdir -p "$(dirname "$log_path")"
    : > "$log_path"
    if [ ! -f "$REGISTRY_TSV" ]; then
        record_skip registry-check registry lint "registry.tsv not found at $REGISTRY_TSV"
        log "registry-check: no registry.tsv"
        return "$EXIT_OK"
    fi
    local known_gates="host bats gui image vm-smoke release-manifest bootstrap-release-profile"
    local line path gate rest rows=0 bad=0
    {
        echo "## registry-check (PILOT — advisory, not enforced)"
        echo "registry: $REGISTRY_TSV"
        echo
    } >> "$log_path"
    while IFS=$'\t' read -r path gate rest; do
        case "$path" in
            ''|'#'*|path) continue ;;   # skip blanks, comments, header
        esac
        rows=$((rows + 1))
        if [ ! -e "$QDISTRO_REPO/$path" ]; then
            printf 'MISS-PATH  %s\n' "$path" >> "$log_path"
            bad=$((bad + 1))
            continue
        fi
        case " $known_gates " in
            *" $gate "*) printf 'OK    %s [%s]\n' "$path" "$gate" >> "$log_path" ;;
            *) printf 'BAD-GATE   %s [%s]\n' "$path" "$gate" >> "$log_path"; bad=$((bad + 1)) ;;
        esac
    done < "$REGISTRY_TSV"
    printf '\n%s rows checked; %s problems (pilot is partial, not yet enforced)\n' \
        "$rows" "$bad" >> "$log_path"
    log "registry-check: $rows rows, $bad problems (advisory)"
    if [ "$bad" -eq 0 ]; then
        record_result registry-check registry.tsv pass 0 pass lint "$log_path" "$rows rows consistent (pilot)"
    else
        record_result registry-check registry.tsv fail "$EXIT_USAGE" args lint "$log_path" "$bad of $rows rows inconsistent"
    fi
    [ "$bad" -eq 0 ] && return "$EXIT_OK"
    return "$EXIT_USAGE"
}
