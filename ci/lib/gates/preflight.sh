#!/usr/bin/env bash
# qci module: preflight gate
# Extracted verbatim from bin/qci. SOURCED by bin/qci into the single
# CI-runner process (shared RDIR/CREATED_VMS/golden state/traps); it is
# NOT executed standalone. See ci/AGENTS.md for the module map.
# shellcheck shell=bash

gate_preflight() {
    qci_assert_run_dir || return $?
    local rc=$EXIT_OK report="$RDIR/preflight/preflight.txt"
    : > "$report"
    check_required() {
        local label=$1 cmd=$2
        if bash -lc "$cmd" >/dev/null 2>&1; then
            printf 'OK\t%s\n' "$label" >> "$report"
            record_result preflight "$label" pass 0 pass tool "$report" ""
        else
            printf 'MISS\t%s\n' "$label" >> "$report"
            record_result preflight "$label" fail "$EXIT_PREFLIGHT" preflight tool "$report" "missing required preflight item"
            rc=$EXIT_PREFLIGHT
        fi
    }
    check_optional() {
        local label=$1 cmd=$2
        if bash -lc "$cmd" >/dev/null 2>&1; then
            printf 'OK\t%s\n' "$label" >> "$report"
            record_result preflight "$label" pass 0 pass tool "$report" ""
        else
            printf 'WARN\t%s\n' "$label" >> "$report"
            record_result preflight "$label" skip 0 pass tool "$report" "optional tool missing; related gate will fail or skip"
        fi
    }
    # Host-test deps are not part of the base install. Surface them up front (as
    # skip/WARN, never failing preflight) so a missing package is visible at the
    # start of a run instead of failing deep in the host gate. Install hint
    # points at qci-host-deps. See README "Host test dependencies".
    check_host_dep() {
        local label=$1 cmd=$2
        if bash -lc "$cmd" >/dev/null 2>&1; then
            printf 'OK\t%s\n' "$label" >> "$report"
            record_result preflight "$label" pass 0 pass tool "$report" ""
        else
            printf 'WARN\t%s\n' "$label" >> "$report"
            record_result preflight "$label" skip 0 pass tool "$report" "missing host-test dep; run qdistro/ci/bin/qci-host-deps --install"
        fi
    }

    local p
    for p in "${PROJECTS[@]}"; do
        check_required "repo $p" "[ -d '$WORKSPACE/$p' ]"
    done
    check_required "python3" "command -v python3"
    check_required "git" "command -v git"
    check_required "bash" "command -v bash"
    check_required "virsh" "command -v virsh"
    check_required "libvirt session" "virsh -c qemu:///session list >/dev/null"
    check_required "vm-exec" "[ -x '$VM_TOOLS/vm-exec' ]"
    check_required "vm-start-and-wait" "[ -x '$VM_TOOLS/vm-start-and-wait' ]"
    check_required "spin-test-vm.sh" "[ -x '$VM_TOOLS/spin-test-vm.sh' ]"
    check_required "baseweed-baked image" "test -f '${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}/baseweed-baked.qcow2'"
    check_optional "bats" "command -v bats"
    check_optional "ruff" "command -v ruff"
    check_optional "mypy" "command -v mypy"
    check_optional "meson" "command -v meson"
    check_optional "ninja" "command -v ninja"
    check_optional "pkg-config" "command -v pkg-config"
    check_optional "npm" "command -v npm"
    check_optional "zola" "command -v zola"
    check_optional "QCI_AGENT_CMD" "test -n \"\${QCI_AGENT_CMD:-}\""
    check_host_dep "host-dep tomli_w (qfileman)" "python3 -c 'import tomli_w'"
    check_host_dep "host-dep libevdev (qdwin)" "pkg-config --exists libevdev"
    check_host_dep "host-dep pango/pangocairo (qdwin)" "pkg-config --exists pango pangocairo"
    check_host_dep "host-dep jeepney (qdbrowser)" "python3 -c 'import jeepney'"
    {
        echo
        echo "## libvirt domains"
        "${VIRSH[@]}" list --all || true
        echo
        echo "## qci overlays"
        ls -1 "${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}"/qci-*.qcow2 2>/dev/null || true
    } >> "$report"
    # Static pre-VM lint runs as part of preflight, but only ever records
    # results (warn/skip) — it must not change preflight's required/optional
    # pass-fail accounting, so we deliberately ignore its return code.
    gate_lint || true
    return "$rc"
}
