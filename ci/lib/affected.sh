#!/usr/bin/env bash
# qci module: registry + changed-path -> gate selection
# Extracted verbatim from bin/qci. SOURCED by bin/qci into the single
# CI-runner process (shared RDIR/CREATED_VMS/golden state/traps); it is
# NOT executed standalone. See ci/AGENTS.md for the module map.
# shellcheck shell=bash

# ---------------------------------------------------------------------------
# Registry helpers shared by `qci affected` and the QCI_OFFLINE annotation.
#
# registry_lookup_gate <path>     -> prints the registry gate for a path, or ""
# registry_network_for <path>     -> prints the registry network column, or ""
# Both read the (TAB-separated) tests/registry.tsv, skipping comments/header.
# ---------------------------------------------------------------------------
registry_field_for() {
    # $1 = repo-relative path; $2 = 1-based column index to print.
    local want=$1 col=$2
    [ -f "$REGISTRY_TSV" ] || return 0
    awk -F'\t' -v want="$want" -v col="$col" '
        /^[[:space:]]*#/ { next }
        $1 == "path" { next }
        $1 == "" { next }
        $1 == want { print $col; exit }
    ' "$REGISTRY_TSV"
}
registry_lookup_gate() { registry_field_for "$1" 2; }
registry_network_for() { registry_field_for "$1" 5; }

# offline_should_skip_external <repo-relative test path>
# Returns 0 (true, "skip it") when QCI_OFFLINE=1 AND the registry marks this
# test's network column as 'external'. This is the statically-knowable half of
# the offline annotation; tests not in the registry rely on the exported
# QCI_OFFLINE=1 env to self-skip at runtime.
offline_should_skip_external() {
    [ "$QCI_OFFLINE" = 1 ] || return 1
    local net
    net=$(registry_network_for "$1")
    [ "$net" = external ]
}

# ---------------------------------------------------------------------------
# Changed-path -> gate selection.
#
# Maps a set of changed file paths (repo-relative, qdistro-rooted) to the qci
# gates that should run. Resolution order per path:
#   1. exact registry row  -> that row's gate;
#   2. else path-prefix rules below;
#   3. else UNKNOWN -> the FULL gate set (fail-safe: never "nothing").
# We never narrow without logging; an unknown path always widens coverage.
#
# Gate vocabulary mirrors the registry/known_gates plus the host/lint pre-VM
# gates and the catch-all 'full'. The emitted set is sorted + de-duplicated.
# ---------------------------------------------------------------------------
# ALL_GATES is the fail-safe FULL coverage set chosen for an unknown/empty
# path (host already runs the runner self-test internally, so selftest is not
# part of the widen-to-full set). GATE_ORDER is the stable emission order and
# includes selftest, which a ci/* or tests/integration/qci/* change selects
# explicitly.
ALL_GATES="lint host release-manifest bootstrap-release-profile image vm-smoke bats gui"
GATE_ORDER="selftest lint host release-manifest bootstrap-release-profile image vm-smoke bats gui"

affected_gates_for_path() {
    # Prints zero or more gate tokens (one per line) for a single path.
    local p=$1 g
    # 1. exact registry hit.
    g=$(registry_lookup_gate "$p")
    if [ -n "$g" ]; then
        printf '%s\n' "$g"
        return 0
    fi
    # 2. conservative path-prefix rules.
    case "$p" in
        # CI runner / registry / lint inputs themselves. A change to the runner
        # or its self-test must re-run the host-only runner self-test.
        ci/*|tests/registry.tsv)
            printf 'selftest\nlint\nhost\n' ;;
        # qci runner self-test bats live here.
        tests/integration/qci/*.bats)
            printf 'selftest\n' ;;
        # Image build / packaging inputs.
        image/*)
            printf 'image\n' ;;
        # Release source-manifest + its R1 tooling -> the release-manifest gate.
        # (NB: source-manifest.txt must be matched here, ahead of the generic
        # *.txt "no gate" rule below.)
        scripts/install/source-manifest.txt|\
        scripts/install/gen-source-manifest.sh|\
        scripts/install/verify-source-manifest.sh)
            printf 'release-manifest\n' ;;
        # The bootstrap installer + its profile lib -> the host-only release
        # bootstrap-profile gate (and release-manifest, since the manifest
        # parser/verify-before-build wiring lives in the bootstrap).
        scripts/install/qdistro-bootstrap.sh|\
        scripts/install/lib/qdistro-profile.sh)
            printf 'bootstrap-release-profile\nrelease-manifest\n' ;;
        # Bootstrap release-contract bats -> the host-only bootstrap-profile gate.
        tests/integration/vm/bootstrap-hardening.bats|\
        tests/integration/vm/source-manifest-signature.bats|\
        tests/integration/vm/gen-source-manifest.bats)
            printf 'bootstrap-release-profile\n' ;;
        # bats VM integration tests.
        tests/integration/vm/*.bats)
            printf 'bats\n' ;;
        # GUI markdown / agent scenarios.
        tests/integration/permissions-gui/*|\
        tests/integration/qdwin-noctalia/*|\
        tests/integration/workflow-gui/*)
            printf 'gui\n' ;;
        # Pure host unit tests.
        tests/unit/*)
            printf 'host\n' ;;
        # VM lifecycle tooling -> exercise a VM boot at least.
        scripts/vm/*)
            printf 'vm-smoke\n' ;;
        # Source under a component dir that the host gate builds/tests.
        src/*|broker/*|selinux/*|qsu/*|workflow/*)
            printf 'host\n' ;;
        # Maintained project docs run the deterministic local-link/anchor lint.
        doc/*|README*)
            printf 'lint\n' ;;
        # Planning notes / changelog: no gate.
        *.md|todo/*|future/*|docs/*|*.txt|LICENSE)
            : ;;
        # 3. anything else is UNKNOWN -> full coverage (fail-safe).
        *)
            printf 'UNKNOWN\n' ;;
    esac
}

# Resolve a list of paths to a sorted, de-duplicated gate set.
# Writes a human-readable mapping to $1 (log file) and prints the final
# space-separated gate list on stdout.
resolve_affected_gates() {
    local log_path=$1; shift
    local p gates_raw="" line saw_unknown=0
    : > "$log_path"
    {
        echo "## affected: changed-path -> gate mapping"
        echo "registry: $REGISTRY_TSV"
        echo
    } >> "$log_path"
    if [ "$#" -eq 0 ]; then
        echo "(no changed paths supplied -> selecting FULL gate set, fail-safe)" >> "$log_path"
        saw_unknown=1
    fi
    for p in "$@"; do
        [ -n "$p" ] || continue
        local out
        out=$(affected_gates_for_path "$p")
        if [ -z "$out" ]; then
            printf '  %-50s -> (no gate; docs/notes)\n' "$p" >> "$log_path"
            continue
        fi
        if printf '%s\n' "$out" | grep -qx UNKNOWN; then
            printf '  %-50s -> UNKNOWN -> FULL set\n' "$p" >> "$log_path"
            saw_unknown=1
            continue
        fi
        while IFS= read -r line; do
            [ -n "$line" ] || continue
            printf '  %-50s -> %s\n' "$p" "$line" >> "$log_path"
            gates_raw="$gates_raw $line"
        done <<EOF
$out
EOF
    done

    local selected
    if [ "$saw_unknown" = 1 ]; then
        # Fail-safe widening: an unknown (or empty) path set means run everything.
        selected="$ALL_GATES"
        {
            echo
            echo "UNKNOWN/empty path encountered -> widening to FULL gate set: $ALL_GATES"
        } >> "$log_path"
    else
        # Order the de-duplicated set by GATE_ORDER for stable output.
        selected=""
        local known g present
        for known in $GATE_ORDER; do
            present=0
            for g in $gates_raw; do
                [ "$g" = "$known" ] && present=1 && break
            done
            [ "$present" = 1 ] && selected="$selected $known"
        done
        # Trim leading space.
        selected="${selected# }"
        # Defensive: if nothing matched (e.g. all docs), DO NOT emit empty and
        # silently skip coverage — widen to full and log it.
        if [ -z "$selected" ]; then
            selected="$ALL_GATES"
            {
                echo
                echo "no gate matched any changed path (all docs/notes?) -> widening to FULL set to avoid silently skipping coverage: $ALL_GATES"
            } >> "$log_path"
        fi
    fi
    {
        echo
        echo "SELECTED GATES: $selected"
    } >> "$log_path"
    printf '%s\n' "$selected"
}

# Run a single named gate (used by `qci affected --run`). Mirrors the main
# dispatch but only for the gate vocabulary affected can emit.
run_one_gate() {
    local gate=$1 vm=$2 step_rc
    case "$gate" in
        selftest) gate_selftest; step_rc=$? ;;
        lint)     gate_lint; step_rc=$? ;;
        host)     gate_host; step_rc=$? ;;
        image)    gate_image; step_rc=$? ;;
        vm-smoke) gate_vm_smoke "$vm"; step_rc=$? ;;
        bats)     gate_bats "$vm"; step_rc=$? ;;
        gui)      gate_gui "$vm"; step_rc=$? ;;
        release-manifest) gate_release_manifest; step_rc=$? ;;
        bootstrap-release-profile) gate_bootstrap_release_profile; step_rc=$? ;;
        *)        record_blocked affected "$gate" "$EXIT_USAGE" args "unknown gate token in affected set"; step_rc=$EXIT_USAGE ;;
    esac
    return "$step_rc"
}

gate_affected() {
    qci_assert_run_dir || return $?
    # Args: <changed-from-ref-or-empty> <run flag 0/1> <vm-or-empty> <paths...>
    local changed_from=$1 do_run=$2 vm=$3; shift 3
    local paths=("$@") log_path="$RDIR/host/affected.log"
    mkdir -p "$(dirname "$log_path")"

    # Derive the changed set from git when --changed-from was given and no
    # explicit paths were passed.
    if [ -n "$changed_from" ] && [ "${#paths[@]}" -eq 0 ]; then
        local diff
        if diff=$(git -C "$QDISTRO_REPO" diff --name-only "$changed_from" 2>/dev/null); then
            while IFS= read -r line; do [ -n "$line" ] && paths+=("$line"); done <<EOF
$diff
EOF
            kv affected_changed_from "$changed_from"
        else
            record_blocked affected "$changed_from" "$EXIT_USAGE" args "git diff --name-only $changed_from failed"
            return "$EXIT_USAGE"
        fi
    fi

    local selected
    selected=$(resolve_affected_gates "$log_path" "${paths[@]}")
    kv affected_gates "$selected"
    cat "$log_path" >&2
    record_result affected gate-selection pass 0 pass selection "$log_path" "selected: $selected"

    if [ "$do_run" != 1 ]; then
        # Selection-only mode: print the gate list to stdout for callers/CI.
        printf '%s\n' "$selected"
        return "$EXIT_OK"
    fi

    # --run: execute the selected gates in ALL_GATES order, accumulating the
    # first non-zero exit class (mirrors gate_full's accumulation).
    local rc=$EXIT_OK g step_rc
    for g in $selected; do
        run_one_gate "$g" "$vm"; step_rc=$?
        [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc
    done
    return "$rc"
}
