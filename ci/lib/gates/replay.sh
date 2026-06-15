#!/usr/bin/env bash
# qci module: replay gate
# Extracted verbatim from bin/qci. SOURCED by bin/qci into the single
# CI-runner process (shared RDIR/CREATED_VMS/golden state/traps); it is
# NOT executed standalone. See ci/AGENTS.md for the module map.
# shellcheck shell=bash

# ---------------------------------------------------------------------------
# qci replay <scenario> <vm>: rerun ONE named scenario against an
# already-preserved (named) VM, WITHOUT provisioning or destroying it.
#
# <scenario> is resolved to one of:
#   - a bats file: an explicit path, or a basename under
#     tests/integration/vm (.bats optional);
#   - a GUI markdown scenario: an explicit path, or a basename matched against
#     the standard scenario roots.
# The VM is validated (must exist; qdistro-daily* refused unless forced) and is
# passed as the explicit --vm to the existing bats/gui dispatch, which never
# creates or releases an explicitly-named VM.
# ---------------------------------------------------------------------------
resolve_replay_scenario() {
    # Prints "<kind>\t<resolved-path>" or nothing. kind = bats|gui.
    local s=$1 cand
    # Explicit, existing path: absolute, relative to CWD, or relative to the
    # repo root. The repo-root fallback keeps resolution CWD-independent (the
    # basename branches below already resolve against $QDISTRO_REPO), so e.g.
    # `qci replay tests/integration/vm/app-launcher.bats <vm>` works from any
    # working directory, not only the repo root.
    local explicit=""
    if [ -f "$s" ]; then
        explicit=$s
    elif [ -f "$QDISTRO_REPO/$s" ]; then
        explicit="$QDISTRO_REPO/$s"
    fi
    if [ -n "$explicit" ]; then
        case "$explicit" in
            *.bats) printf 'bats\t%s\n' "$explicit" ;;
            *.md)   printf 'gui\t%s\n' "$explicit" ;;
            *)      : ;;
        esac
        return 0
    fi
    # bats basename (with or without .bats) under tests/integration/vm.
    local base=${s%.bats}
    cand="$QDISTRO_REPO/tests/integration/vm/$base.bats"
    if [ -f "$cand" ]; then
        printf 'bats\t%s\n' "$cand"
        return 0
    fi
    # GUI markdown basename across the known scenario roots. This list MUST
    # cover every root agent_scenarios enumerates (qdwin tests/gui + tests/apps,
    # qdistro permissions-gui + qdwin-noctalia, qdlocker tests/gui) — otherwise
    # `qci replay <basename>` cannot find a scenario that `qci gui` actually
    # runs. workflow-gui is also accepted (a valid replay target). Defined once
    # and reused by both the exact-match and the glob-prefix passes below.
    local root mdbase=${s%.md}
    local gui_roots=(
        "$WORKSPACE/qdwin/tests/gui"
        "$WORKSPACE/qdwin/tests/apps"
        "$QDISTRO_REPO/tests/integration/permissions-gui"
        "$QDISTRO_REPO/tests/integration/qdwin-noctalia"
        "$WORKSPACE/qdlocker/tests/gui"
        "$QDISTRO_REPO/tests/integration/workflow-gui"
    )
    for root in "${gui_roots[@]}"; do
        cand="$root/$mdbase.md"
        [ -f "$cand" ] && { printf 'gui\t%s\n' "$cand"; return 0; }
    done
    # Glob fallback: a basename prefix match (e.g. "03" -> 03-*.md).
    for root in "${gui_roots[@]}"; do
        for cand in "$root/$mdbase"*.md; do
            [ -f "$cand" ] && { printf 'gui\t%s\n' "$cand"; return 0; }
        done
    done
    return 0
}

gate_replay() {
    qci_assert_run_dir || return $?
    local scenario=$1 vm=$2 kind path
    if [ -z "$scenario" ] || [ -z "$vm" ]; then
        record_blocked replay "${scenario:-<missing>}" "$EXIT_USAGE" args "usage: qci replay <scenario> <vm>"
        return "$EXIT_USAGE"
    fi
    # The VM must already exist; replay never provisions. validate_vm also
    # enforces the qdistro-daily* protection (override via QCI_FORCE_PROTECTED_VM).
    if ! validate_vm replay "$vm"; then
        return "$EXIT_VM_PROVISION"
    fi
    local resolved
    resolved=$(resolve_replay_scenario "$scenario")
    if [ -z "$resolved" ]; then
        record_blocked replay "$scenario" "$EXIT_USAGE" args "scenario not found (no bats/gui match)"
        return "$EXIT_USAGE"
    fi
    kind=${resolved%%$'\t'*}
    path=${resolved#*$'\t'}
    kv replay_scenario "$path"
    kv replay_kind "$kind"
    kv replay_vm "$vm"
    log "replay $kind scenario '$path' against existing VM '$vm'"
    case "$kind" in
        bats)
            gate_bats "$vm" "$path"
            return $?
            ;;
        gui)
            # Restrict gate_gui to just this one scenario by overriding the
            # scenario producer, exactly like `qci gui --scenario` does.
            agent_scenarios() { printf '%s\n' "$path"; }
            gate_gui "$vm"
            return $?
            ;;
        *)
            record_blocked replay "$scenario" "$EXIT_USAGE" args "unresolvable scenario kind"
            return "$EXIT_USAGE"
            ;;
    esac
}
