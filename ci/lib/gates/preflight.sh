#!/usr/bin/env bash
# qci module: preflight gate
# Extracted verbatim from bin/qci. SOURCED by bin/qci into the single
# CI-runner process (shared RDIR/CREATED_VMS/golden state/traps); it is
# NOT executed standalone. See ci/AGENTS.md for the module map.
# shellcheck shell=bash

# Pure disk-space floor decision (H7). Given free GiB and a floor GiB, echo the
# verdict token and set the return code:
#   ok    (rc 0)  free >= 2*floor          — plenty of headroom
#   warn  (rc 0)  floor <= free < 2*floor  — pass, but flag the thin margin
#   low   (rc 1)  free < floor             — preflight must fail
# Host-testable: no I/O, only integer args.
disk_space_verdict() {
    local free=$1 floor=$2
    if [ "$free" -lt "$floor" ]; then echo low; return 1; fi
    if [ "$free" -lt "$((floor * 2))" ]; then echo warn; return 0; fi
    echo ok
    return 0
}

# Free GiB (GiB = 1024^3) on the filesystem holding $path, walking up to the
# nearest existing ancestor first (the run dir / an overlay may not exist yet).
# Echoes an integer, or nothing if df fails.
fs_free_gib() {
    local path=$1 p
    p=$path
    while [ -n "$p" ] && [ "$p" != "/" ] && [ ! -e "$p" ]; do p=$(dirname "$p"); done
    [ -n "$p" ] || p=/
    df -PB1G "$p" 2>/dev/null | awk 'NR==2{print $4+0}'
}

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

    # Disk-space floor (H7): record measured free GiB and fail below the floor.
    check_disk_space() {
        local label=$1 free=$2 floor=$3 verdict note
        if [ -z "$free" ]; then
            printf 'WARN\tdisk %s: free unknown\n' "$label" >> "$report"
            record_result preflight "disk $label" skip 0 pass tool "$report" "free space unknown (df failed)"
            return
        fi
        verdict=$(disk_space_verdict "$free" "$floor") || true
        note="free=${free}GiB floor=${floor}GiB"
        case "$verdict" in
            low)
                printf 'FAIL\tdisk %s: %s\n' "$label" "$note" >> "$report"
                record_result preflight "disk $label" fail "$EXIT_PREFLIGHT" preflight tool "$report" "free disk below floor ($note); set QCI_MIN_FREE_GIB to adjust"
                rc=$EXIT_PREFLIGHT ;;
            warn)
                printf 'WARN\tdisk %s: %s (< 2x floor)\n' "$label" "$note" >> "$report"
                record_result preflight "disk $label" pass 0 pass tool "$report" "free disk within 2x floor ($note)" ;;
            *)
                printf 'OK\tdisk %s: %s\n' "$label" "$note" >> "$report"
                record_result preflight "disk $label" pass 0 pass tool "$report" "$note" ;;
        esac
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
    # Disk-space floor: the libvirt images volume (goldens + worker overlays) and
    # the run-dir filesystem. Floor justified by observed artifact sizes — a bats
    # golden backing is ~5-6 GiB, the two GUI goldens ~0.8 GiB each, plus a pool of
    # up to QCI_JOBS worker overlays that grow to hundreds of MiB (tiered-isolation
    # reached ~370 MiB); realistically 8-12 GiB of live disk per run. Default
    # 20 GiB leaves comfortable headroom; override with QCI_MIN_FREE_GIB. The
    # images volume is derived from the actual baked backing image if present
    # (its filesystem is where goldens/overlays land), else QDWIN_IMG_DIR.
    local min_free=${QCI_MIN_FREE_GIB:-20} img_dir img_ref
    img_dir="${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}"
    img_ref="$img_dir/baseweed-baked.qcow2"
    [ -e "$img_ref" ] || img_ref="$img_dir"
    check_disk_space "images volume ($img_dir)" "$(fs_free_gib "$img_ref")" "$min_free"
    check_disk_space "run dir ($RDIR)" "$(fs_free_gib "$RDIR")" "$min_free"
    check_optional "bats" "command -v bats"
    check_optional "ruff" "command -v ruff"
    check_optional "mypy" "command -v mypy"
    check_optional "meson" "command -v meson"
    check_optional "ninja" "command -v ninja"
    check_optional "pkg-config" "command -v pkg-config"
    check_optional "npm" "command -v npm"
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
