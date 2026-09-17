#!/usr/bin/env bash
# qci module: gui gate + agent scenarios
# Extracted verbatim from bin/qci. SOURCED by bin/qci into the single
# CI-runner process (shared RDIR/CREATED_VMS/golden state/traps); it is
# NOT executed standalone. See ci/AGENTS.md for the module map.
# shellcheck shell=bash

run_qdwin_executable_gui_smokes() {
    local vm=$1 qdwin_capture=${2:-1} rc=$EXIT_OK scenario file step_rc
    export VMNAME="$vm"
    # Every executable smoke takes at least one qdwin_screenshot(), which now
    # requires the golden's shell-capture bake. On an old golden, skip the
    # WHOLE lane with the same rebake hint the vision/markdown lanes use —
    # one consistent capability signal, no partial hard-fails.
    if [ "$qdwin_capture" = 0 ]; then
        for scenario in \
            agent-mvp-session-smoke.sh \
            agent-protocol-audit.sh \
            agent-cursor-clickthrough-smoke.sh \
            agent-click-smoke.sh \
            agent-vendored-libweston-verify.sh \
            agent-shell-capture-smoke.sh
        do
            record_result gui "qdwin-$scenario" skip 0 pass gui "" \
                "golden lacks QDWIN_ENABLE_SHELL_CAPTURE=1 (qdwin_screenshot needs the shell-capture path); rebake the golden with fresh-vm-bootstrap"
        done
        return 0
    fi
    if [ "${QCI_GUI_SKIP_QDWIN:-0}" = 1 ]; then
        for scenario in \
            agent-mvp-session-smoke.sh \
            agent-protocol-audit.sh \
            agent-cursor-clickthrough-smoke.sh \
            agent-click-smoke.sh
        do
            record_result gui "qdwin-$scenario" skip 0 pass gui "" "QCI_GUI_SKIP_QDWIN=1: qdwin-dependent smoke skipped"
        done
        record_result gui "qdwin-agent-vendored-libweston-verify.sh" skip 0 pass gui "" "QCI_GUI_SKIP_QDWIN=1: qdwin-dependent smoke skipped"
        record_result gui "qdwin-agent-shell-capture-smoke.sh" skip 0 pass gui "" "QCI_GUI_SKIP_QDWIN=1: qdwin-dependent smoke skipped"
        return 0
    fi
    if ! "$VM_TOOLS/vm-exec" "$vm" "test -S /run/user/1000/wayland-1 && ! pgrep -x labwc >/dev/null && runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user is-active qdwin-compositor.service qdshell.service qdistro-cursor-sprites.service >/dev/null" >/dev/null 2>&1; then
        for scenario in \
            agent-mvp-session-smoke.sh \
            agent-protocol-audit.sh \
            agent-cursor-clickthrough-smoke.sh \
            agent-click-smoke.sh
        do
            record_result gui "qdwin-$scenario" skip 0 pass gui "" "qdwin production session not active in this VM profile"
        done
        record_result gui "qdwin-agent-vendored-libweston-verify.sh" skip 0 pass gui "" "qdwin production session not active in this VM profile"
        record_result gui "qdwin-agent-shell-capture-smoke.sh" skip 0 pass gui "" "qdwin production session not active in this VM profile"
        return 0
    fi
    for scenario in \
        agent-mvp-session-smoke.sh \
        agent-protocol-audit.sh \
        agent-cursor-clickthrough-smoke.sh \
        agent-click-smoke.sh
    do
        file="$WORKSPACE/qdwin/tests/gui/$scenario"
        [ -x "$file" ] || {
            record_blocked gui "$scenario" "$EXIT_GUI" gui "scenario script missing or not executable"
            [ "$rc" -eq 0 ] && rc=$EXIT_GUI
            continue
        }
        run_logged gui "qdwin-$scenario" "$EXIT_GUI" gui "$WORKSPACE/qdwin" "VMNAME='$vm' '$file'" ""; step_rc=$?
        [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc
    done

    # agent-vendored-libweston-verify.sh exercises the layer-popup grab
    # paths that ONLY work against qdistro's patched libweston. Precheck
    # which libweston the session loaded: only run (and let it gate the
    # pipeline) when the vendored tree is in force. On a stock-libweston
    # VM profile, skip rather than fail — the verify script's exit 2
    # (SETUP) would otherwise map to a hard GUI failure.
    local vlw_file vlw_prefix loaded_lw
    vlw_file="$WORKSPACE/qdwin/tests/gui/agent-vendored-libweston-verify.sh"
    vlw_prefix="${QDWIN_VENDORED_LIBWESTON_PREFIX:-/usr/libexec/qdistro/qdwin-libweston}"
    loaded_lw=$("$VM_TOOLS/vm-exec" "$vm" "pmap \$(pgrep -x weston | head -n1) 2>/dev/null | grep -o '/[^ ]*libweston-[0-9]*\.so[^ ]*' | sort -u | head -n1" 2>/dev/null | grep -v '^\[vm-exec\]' | tr -d '\r')
    if [ ! -x "$vlw_file" ]; then
        record_blocked gui "agent-vendored-libweston-verify.sh" "$EXIT_GUI" gui "scenario script missing or not executable"
        [ "$rc" -eq 0 ] && rc=$EXIT_GUI
    elif [ -z "$loaded_lw" ] || [ "${loaded_lw#"$vlw_prefix"}" = "$loaded_lw" ]; then
        record_result gui "qdwin-agent-vendored-libweston-verify.sh" skip 0 pass gui "" \
            "session not running vendored libweston (loaded: ${loaded_lw:-unknown}); layer-popup grab discriminators N/A"
    else
        run_logged gui "qdwin-agent-vendored-libweston-verify.sh" "$EXIT_GUI" gui "$WORKSPACE/qdwin" "VMNAME='$vm' '$vlw_file'" ""; step_rc=$?
        [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc
    fi

    # agent-shell-capture-smoke.sh gates the in-compositor shell-authorized
    # capture path (the ONLY sanctioned visual-evidence source — virsh only
    # sees the tty on these headless VMs). It requires a golden whose
    # compositor unit sets QDWIN_ENABLE_SHELL_CAPTURE=1 (fresh-vm-bootstrap
    # bakes this); on an older golden, skip with a rebake hint rather than
    # hard-fail the pipeline.
    local sc_file sc_env
    sc_file="$WORKSPACE/qdwin/tests/gui/agent-shell-capture-smoke.sh"
    sc_env=$("$VM_TOOLS/vm-exec" "$vm" "pid=\$(runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user show qdwin-compositor.service -p MainPID --value); tr '\0' '\n' </proc/\$pid/environ 2>/dev/null | grep -c '^QDWIN_ENABLE_SHELL_CAPTURE=1\$'" 2>/dev/null | grep -v '^\[vm-exec\]' | tr -d '\r')
    if [ ! -x "$sc_file" ]; then
        record_blocked gui "agent-shell-capture-smoke.sh" "$EXIT_GUI" gui "scenario script missing or not executable"
        [ "$rc" -eq 0 ] && rc=$EXIT_GUI
    elif [ "${sc_env:-0}" != "1" ]; then
        record_result gui "qdwin-agent-shell-capture-smoke.sh" skip 0 pass gui "" \
            "compositor lacks QDWIN_ENABLE_SHELL_CAPTURE=1 (golden predates shell capture; rebake with fresh-vm-bootstrap)"
    else
        run_logged gui "qdwin-agent-shell-capture-smoke.sh" "$EXIT_GUI" gui "$WORKSPACE/qdwin" "VMNAME='$vm' '$sc_file'" ""; step_rc=$?
        [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc
    fi
    return "$rc"
}

# Return a stable, logical workspace-relative identity for a GUI scenario.
#
# Explicit --scenario arguments may name the canonical target of a workspace
# symlink (for example /home/me/ws/qdwin/... while WORKSPACE/qdwin is a
# symlink), and qdistro itself may be running from a git worktree outside the
# normal WORKSPACE/qdistro path.  Raw prefix stripping misclassified those
# paths as gui-admin, so qdwin/qdlocker scenarios booted the wrong VM profile.
# Canonicalize both sides and map known project roots back to project/path.
gui_scenario_rel() {
    local scenario=$1 canonical root project repo_var repo
    canonical=$(readlink -f -- "$scenario" 2>/dev/null) || canonical=$scenario

    for project in qdistro qdwin qdshell qdlocker; do
        if [ "$project" = qdistro ]; then
            repo=${QDISTRO_REPO:-}
        else
            repo_var=$(printf '%s' "$project" | tr '[:lower:]-' '[:upper:]_')_REPO
            repo=${!repo_var:-${WORKSPACE:-}/$project}
        fi
        [ -n "$repo" ] || continue
        root=$(readlink -f -- "$repo" 2>/dev/null) || root=$repo
        case "$canonical" in
            "$root"/*)
                printf '%s/%s\n' "$project" "${canonical#"$root"/}"
                return 0
                ;;
        esac
    done

    case "$scenario" in
        "${WORKSPACE:-}"/*) printf '%s\n' "${scenario#"$WORKSPACE"/}" ;;
        *) printf '%s\n' "$scenario" ;;
    esac
}

gui_scenario_requires_qdwin() {
    local rel=$1
    case "$rel" in
        qdwin/tests/gui/[0-9][0-9]-*.md|\
        qdwin/tests/apps/[0-9][0-9]-*.md|\
        qdistro/tests/integration/qdwin-noctalia/[0-9][0-9]-*.md|\
        tests/integration/qdwin-noctalia/[0-9][0-9]-*.md|\
        qdlocker/tests/gui/[0-9][0-9]-*.md|\
        qdistro/tests/integration/permissions-gui/18-podapps-launcher-badge.md|\
        tests/integration/permissions-gui/18-podapps-launcher-badge.md|\
        qdistro/tests/integration/permissions-gui/19-tier5-loopback-visible.md|\
        tests/integration/permissions-gui/19-tier5-loopback-visible.md|\
        qdistro/tests/integration/permissions-gui/20-tier5-vm-cold-start.md|\
        tests/integration/permissions-gui/20-tier5-vm-cold-start.md|\
        qdistro/tests/integration/permissions-gui/21-tier5-close-cleanup.md|\
        tests/integration/permissions-gui/21-tier5-close-cleanup.md|\
        qdistro/tests/integration/permissions-gui/56-tier4-rdp-window-visible.md|\
        tests/integration/permissions-gui/56-tier4-rdp-window-visible.md|\
        qdistro/tests/integration/permissions-gui/57-tier4-rdp-close-cleanup.md|\
        tests/integration/permissions-gui/57-tier4-rdp-close-cleanup.md)
            return 0 ;;
        *)
            return 1 ;;
    esac
}

# True (0) when a qdwin GUI scenario drives the REMOVED legacy qdshell.py
# ctrl-socket — detected by CONTENT: the `qdwin_ctrl` shell helper or a raw
# `socat … /run/user/1000/qdshell.sock` call. The shipping session is Quickshell,
# whose ctrl-server (qdshell/qml-plugin/ctrl-server.cpp) only answers
# status/last-overlay-keys, so every legacy verb returns "error: unknown command"
# and these scenarios can never pass. They used to be hidden because the whole
# qdwin profile was skipped; commit "ci(gui): route qdwin scenarios to qdwin
# profile" began running them live, turning them into agent ERRORs. We skip them
# deterministically by content (so MODERN qs-ipc scenarios — e.g. gui/17,18 — and
# app-launch tests still run), unless the explicit legacy lane is requested with
# QCI_GUI_RUN_LEGACY_QDWIN_MD=1. Detection is content-based rather than the old
# runtime `legacy_ctrl` probe, which was unsound (it ran on the gui-admin VM while
# the scenarios run on gui-qdwin, and the modern qs server owns the same socket
# path, so the probe false-positived across profiles and flipped a green run red).
# Arg: absolute or workspace-relative scenario path.
gui_scenario_uses_legacy_ctrl() {
    local file=$1
    case "$file" in
        */qdwin/tests/gui/[0-9][0-9]-*.md|*/qdwin/tests/apps/[0-9][0-9]-*.md|\
        qdwin/tests/gui/[0-9][0-9]-*.md|qdwin/tests/apps/[0-9][0-9]-*.md) ;;
        *) return 1 ;;
    esac
    [ -f "$file" ] || return 1
    grep -qE 'qdwin_ctrl|socat[^|]*qdshell\.sock' "$file"
}

# Enumerate every GUI scenario file across the workspace. qdistro's own two
# directories are anchored on $QDISTRO_REPO, not "$WORKSPACE"/qdistro: from a
# renamed checkout the latter dispatched the CANONICAL sibling's scenarios (and,
# via collect_repo_state, recorded the canonical HEAD), and from a linked
# worktree it found none at all and the gate passed on siblings alone.
# gui_scenario_rel was already fixed for this; these two globs were missed.
agent_scenarios() {
    local f
    for f in \
        "$WORKSPACE"/qdwin/tests/gui/[0-9][0-9]-*.md \
        "$WORKSPACE"/qdwin/tests/apps/[0-9][0-9]-*.md \
        "$QDISTRO_REPO"/tests/integration/permissions-gui/[0-9][0-9]-*.md \
        "$QDISTRO_REPO"/tests/integration/qdwin-noctalia/[0-9][0-9]-*.md \
        "$WORKSPACE"/qdlocker/tests/gui/[0-9][0-9]-*.md
    do
        [ -f "$f" ] && printf '%s\n' "$f"
    done
}

# Reject an explicit scenario typo before qci builds a golden, starts a VM, or
# launches a GUI-capable agent. The normal scenario producer emits only files
# found by globs, but `qci gui --scenario ...` replaces it with operator input;
# letting a missing path reach the worker used to spend a full image bake and
# then invite the model to improvise a different scenario.
# It also validates the MANDATORY `<!-- qci:visual: required|none -->`
# declaration on every scenario it dispatches. That declaration decides whether
# the visual-evidence contract applies, so a missing/unknown/conflicting one is
# a registry defect, not a runtime surprise: caught here it costs a usage error,
# caught at grading time it costs a whole agent attempt recorded as ERROR.
gui_validate_scenarios() {
    local scenario rc=0 vmode
    while IFS= read -r scenario; do
        if [ -z "$scenario" ]; then
            record_blocked gui '<missing>' "$EXIT_USAGE" args \
                "--scenario requires an existing readable .md file"
            rc=$EXIT_USAGE
            continue
        elif [ "${scenario##*.}" != md ] || [ ! -f "$scenario" ] || [ ! -r "$scenario" ]; then
            record_blocked gui "$scenario" "$EXIT_USAGE" args \
                "GUI scenario must be an existing readable .md file; rejected before VM provisioning"
            rc=$EXIT_USAGE
            continue
        fi
        vmode=$(gui_scenario_visual_mode "$scenario")
        case "$vmode" in
            required|none) ;;
            *)
                record_blocked gui "$scenario" "$EXIT_USAGE" args \
                    "GUI scenario visual declaration: ${vmode#invalid:}; add exactly one <!-- qci:visual: required --> (a required assertion is decided by pixels) or <!-- qci:visual: none --> (no required assertion is) to the scenario"
                rc=$EXIT_USAGE
                ;;
        esac
    done < <(agent_scenarios)
    return "$rc"
}

# Short, stable host alias for a canonical per-scenario artifact dir.
# Agents under load routinely mangle long `ci/runs/full-<stamp>-<pid>/gui/<slug>`
# paths (drop the pid suffix, invent siblings). A fixed /tmp/qci-gui-art/<16hex>
# real directory is short and unique per adir (collision-safe under
# QCI_GUI_JOBS=N). It must not be a symlink: hardened ImageMagick policies deny
# output through symlinked path components, which previously tempted an agent to
# replace the alias and strand an otherwise valid PASS outside the run dir.
# gui_harvest_agent_artifacts copies the attempt evidence into the canonical dir.
# Pure enough for host selftests when TMPDIR is redirected.
# Args: absolute canonical artifact dir. Echoes the alias path.
gui_make_artifact_alias() {
    local adir=$1 key base short target_file owner
    [ -n "$adir" ] || return 1
    mkdir -p "$adir" || return 1
    key=$(printf '%s' "$adir" | sha256sum 2>/dev/null | awk '{print substr($1,1,16)}')
    [ -n "$key" ] || key=$(printf '%s' "$adir" | cksum | awk '{print $1}')
    base="${QCI_GUI_ART_ALIAS_ROOT:-${TMPDIR:-/tmp}/qci-gui-art}"
    if [ -L "$base" ]; then
        return 1
    elif [ -e "$base" ]; then
        [ -d "$base" ] || return 1
        owner=$(stat -c %u "$base" 2>/dev/null || true)
        [ "$owner" = "$(id -u)" ] || return 1
        chmod 0700 "$base" || return 1
    else
        mkdir -m 0700 "$base" || return 1
    fi
    short="$base/$key"
    target_file="$base/$key.target"
    # Never reuse a real directory or sidecar: stale PASS/evidence from a
    # repeated/interrupted setup must not become this attempt's result.
    [ ! -e "$target_file" ] && [ ! -L "$target_file" ] || return 1
    # A key is a content hash of the full adir path, so any pre-existing entry
    # belongs to a repeated/interrupted setup. Refuse both historical symlinks
    # and real directories; the caller safely falls back to the canonical path.
    [ ! -e "$short" ] && [ ! -L "$short" ] || return 1
    mkdir -m 0700 "$short" || return 1
    if ! (set -o noclobber; printf '%s\n' "$adir" > "$target_file") 2>/dev/null; then
        rmdir "$short" 2>/dev/null || true
        return 1
    fi
    printf '%s\n' "$short"
}

# Remove a successfully harvested real-directory alias. The exact sidecar
# mapping and containment checks make the recursive removal fail closed; an
# untrusted/malformed path is left behind for an operator instead of widened.
# Args: short_alias_dir canonical_adir
gui_remove_artifact_alias() {
    local short=$1 adir=$2 base real_short real_base target owner
    [ -n "$short" ] && [ -n "$adir" ] || return 1
    [ -d "$short" ] && [ ! -L "$short" ] || return 1
    [ -f "$short.target" ] && [ ! -L "$short.target" ] || return 1
    target=$(cat "$short.target" 2>/dev/null || true)
    [ "$target" = "$adir" ] || return 1
    base=$(dirname "$short")
    real_short=$(readlink -f "$short" 2>/dev/null || true)
    real_base=$(readlink -f "$base" 2>/dev/null || true)
    [ -n "$real_short" ] && [ -n "$real_base" ] || return 1
    owner=$(stat -c %u "$real_short" 2>/dev/null || true)
    [ "$owner" = "$(id -u)" ] || return 1
    case "$real_short" in
        "$real_base"/*) ;;
        *) return 1 ;;
    esac
    rm -rf -- "$real_short" || return 1
    rm -f -- "$short.target" || return 1
}

# Rewrite report-style links after evidence moves from the short alias to the
# canonical run directory. Only exact alias prefixes in human-authored report
# files are changed; raw logs and screenshots remain byte-for-byte evidence.
gui_rebase_artifact_report_links() {
    local adir=$1 alias_real=$2 f
    for f in report.md report.txt debug.md notes.txt; do
        [ -e "$adir/$f" ] || continue
        [ -f "$adir/$f" ] && [ ! -L "$adir/$f" ] || return 1
        python3 - "$adir/$f" "$alias_real/" <<'PY' || return 1
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
old = sys.argv[2].encode()
data = path.read_bytes()
if old in data:
    path.write_bytes(data.replace(old, b"./"))
PY
    done
}

# Read first verdict token from a status.txt (or empty).
gui_status_file_verdict() {
    local f=$1 raw=""
    [ -f "$f" ] && [ ! -L "$f" ] || { printf '\n'; return 0; }
    raw=$(tr -d '\r' < "$f" | awk 'NF {print toupper($1); exit}')
    case "$raw" in
        PASSN) raw=PASS ;;
        FAILN) raw=FAIL ;;
        ERRORN) raw=ERROR ;;
        SKIPN) raw=SKIP ;;
    esac
    case "$raw" in
        PASS|FAIL|ERROR|SKIP) printf '%s\n' "$raw" ;;
        *) printf '\n' ;;
    esac
}

gui_mark_harvest_invalid() {
    local adir=$1 log_path=$2 reason=$3
    printf '%s\n' "$reason" > "$adir/.harvest-invalid" 2>/dev/null || true
    printf 'gui_harvest: INVALID: %s\n' "$reason" \
        >> "${log_path:-/dev/null}" 2>/dev/null || true
}

# After an agent attempt: if the canonical adir has no usable status.txt, search
# for a misplaced one for THIS slug only and copy evidence into the canonical
# dir. Designed for dumb agents + concurrent GUI pools:
#   * only paths whose parent directory basename is exactly `$slug` (never
#     another scenario's artifacts)
#   * refuses to harvest when candidates disagree on PASS/FAIL/ERROR/SKIP
#   * prefers a candidate under the real RDIR, then under the timestamp-only
#     truncated sibling (the observed Luna failure mode), then newest mtime
# Returns 0 always (best-effort); leaves a `.harvested-from` breadcrumb on
# success so report triage can see the recovery. Host-testable.
# Args: canonical_adir slug [agent_log] [short_alias_dir]
gui_harvest_agent_artifacts() {
    local adir=$1 slug=$2 log_path=${3:-} alias_dir=${4:-}
    local st="" alias_st="" cand="" cands=() seen="" v="" vs="" picked="" parent runs_parent rid rid_ts p
    local alias_real="" alias_target="" alias_base="" alias_base_real="" alias_valid=0
    [ -n "$adir" ] && [ -n "$slug" ] || return 0
    mkdir -p "$adir" 2>/dev/null || true

    # Authenticate the harness-created real alias before considering a verdict
    # written elsewhere. If the agent swaps/removes it, canonical prose/status
    # must not bypass the short-path contract. `alias_dir == adir` is the
    # intentional fallback when alias creation itself failed before the agent.
    if [ -n "$alias_dir" ] && [ "$alias_dir" != "$adir" ]; then
        alias_real=$(readlink -f "$alias_dir" 2>/dev/null || printf '%s' "$alias_dir")
        alias_base="${QCI_GUI_ART_ALIAS_ROOT:-${TMPDIR:-/tmp}/qci-gui-art}"
        alias_base_real=$(readlink -f "$alias_base" 2>/dev/null || printf '%s' "$alias_base")
        if [ -f "$alias_dir.target" ] && [ ! -L "$alias_dir.target" ]; then
            alias_target=$(cat "$alias_dir.target" 2>/dev/null || true)
        fi
        if [ "$alias_target" = "$adir" ] && [ -d "$alias_dir" ] && [ ! -L "$alias_dir" ] \
                && [ "$(stat -c %u "$alias_dir" 2>/dev/null || true)" = "$(id -u)" ]; then
            case "$alias_real" in
                "$alias_base_real"/*) alias_valid=1 ;;
            esac
        fi
        if [ "$alias_valid" -ne 1 ]; then
            gui_mark_harvest_invalid "$adir" "$log_path" \
                "artifact alias authentication failed: $alias_dir"
            return 0
        fi
        if [ -e "$alias_real/status.txt" ] || [ -L "$alias_real/status.txt" ]; then
            if [ ! -f "$alias_real/status.txt" ] || [ -L "$alias_real/status.txt" ]; then
                gui_mark_harvest_invalid "$adir" "$log_path" \
                    "artifact alias status is not a regular non-symlink file: $alias_real/status.txt"
                return 0
            fi
            alias_st=$(gui_status_file_verdict "$alias_real/status.txt")
            if [ -z "$alias_st" ]; then
                gui_mark_harvest_invalid "$adir" "$log_path" \
                    "artifact alias status has no usable verdict: $alias_real/status.txt"
                return 0
            fi
        fi
    fi

    if [ -L "$adir/status.txt" ] || [ -L "$adir/report.md" ]; then
        gui_mark_harvest_invalid "$adir" "$log_path" \
            "canonical verdict artifact is a symlink"
        return 0
    fi
    st=$(gui_status_file_verdict "$adir/status.txt")
    if [ -f "$adir/report.md" ] && [ ! -f "$adir/status.txt" ]; then
        st=$(agent_artifact_status "$adir" "${log_path:-/dev/null}")
        case "$st" in PASS|FAIL|ERROR|SKIP) ;; *) st="" ;; esac
    fi
    if [ -n "$st" ] && [ -n "$alias_st" ] && [ "$st" != "$alias_st" ]; then
        gui_mark_harvest_invalid "$adir" "$log_path" \
            "canonical verdict $st conflicts with authenticated alias verdict $alias_st"
        return 0
    fi
    if [ -n "$st" ] && [ -z "$alias_st" ]; then
        return 0
    fi

    # --- collect candidates (status.txt only; slug-scoped) ---
    # Intentionally NOT a recursive find over all of RUNS_DIR: historical runs
    # for the same slug routinely disagree (PASS vs FAIL) and would veto recovery
    # under concurrent/long-lived CI hosts. Only THIS attempt's neighborhoods.
    #
    # 1) paths mentioned in the agent log (normalize accidental // prefixes
    #    from greedy path extractors)
    if [ -n "$log_path" ] && [ -f "$log_path" ]; then
        while IFS= read -r cand; do
            [ -n "$cand" ] || continue
            while [[ "$cand" == //* ]]; do cand=${cand#/}; done
            cands+=("$cand")
        done < <(grep -oE '/[[:alnum:]./_%+-]+/gui/'"$slug"'/status\.txt' "$log_path" 2>/dev/null \
            | sort -u || true)
    fi
    # 2) known Luna truncation: drop trailing -<pid> from run id
    #    (only a pure-digit suffix — never eat the ISO timestamp)
    if [ -n "${RDIR:-}" ]; then
        runs_parent=$(dirname "$RDIR")
        rid=$(basename "$RDIR")
        rid_ts=$rid
        if [[ "$rid" =~ ^(.*)-([0-9]+)$ ]]; then
            rid_ts="${BASH_REMATCH[1]}"
        fi
        if [ -n "$rid_ts" ] && [ "$rid_ts" != "$rid" ]; then
            cands+=("$runs_parent/$rid_ts/gui/$slug/status.txt")
        fi
        # also: agent may have written under RDIR but a slightly wrong subdir
        cands+=("$RDIR/gui/$slug/status.txt")
    fi
    # 3) the already-authenticated short real-directory alias.
    if [ "$alias_valid" -eq 1 ] && [ -n "$alias_st" ]; then
        cands+=("$alias_real/status.txt")
    fi

    # Dedup + validate: parent basename == slug, file exists, not already adir
    local -a valid=()
    local adir_real
    adir_real=$(readlink -f "$adir" 2>/dev/null || printf '%s' "$adir")
    seen=$'\n'
    for cand in "${cands[@]+"${cands[@]}"}"; do
        while [[ "$cand" == //* ]]; do cand=${cand#/}; done
        [ -f "$cand" ] && [ ! -L "$cand" ] || continue
        parent=$(basename "$(dirname "$cand")")
        if [ "$parent" != "$slug" ]; then
            [ "$alias_valid" -eq 1 ] && [ "$cand" = "$alias_real/status.txt" ] || continue
        fi
        # resolve to real path for comparison when possible
        p=$(readlink -f "$cand" 2>/dev/null || printf '%s' "$cand")
        case "$seen" in
            *$'\n'"$p"$'\n'*) continue ;;
        esac
        seen+="$p"$'\n'
        # skip if already inside the canonical adir (resolved)
        case "$p" in
            "$adir_real"|"$adir_real"/*) continue ;;
        esac
        v=$(gui_status_file_verdict "$cand")
        [ -n "$v" ] || continue
        # store the resolved path so prefer-matching is slash-stable
        valid+=("$p")
    done

    [ "${#valid[@]}" -gt 0 ] || return 0

    # Prefer the harness-authenticated alias, then real RDIR, then the truncated
    # timestamp sibling of THIS run.
    # A preferred path is trusted alone (other historical runs for the same
    # slug may disagree and must not veto recovery of this attempt).
    picked=""
    if [ "$alias_valid" -eq 1 ] && [ -f "$alias_real/status.txt" ]; then
        picked="$alias_real/status.txt"
    fi
    if [ -z "$picked" ] && [ -n "${RDIR:-}" ]; then
        local rdir_real trunc_real
        rdir_real=$(readlink -f "$RDIR" 2>/dev/null || printf '%s' "$RDIR")
        for cand in "${valid[@]}"; do
            case "$cand" in
                "$rdir_real"/*|"$RDIR"/*) picked=$cand; break ;;
            esac
        done
        if [ -z "$picked" ]; then
            rid=$(basename "$RDIR")
            rid_ts=$rid
            if [[ "$rid" =~ ^(.*)-([0-9]+)$ ]]; then
                rid_ts="${BASH_REMATCH[1]}"
            fi
            runs_parent=$(dirname "$RDIR")
            if [ "$rid_ts" != "$rid" ]; then
                trunc_real=$(readlink -f "$runs_parent/$rid_ts" 2>/dev/null || printf '%s' "$runs_parent/$rid_ts")
                for cand in "${valid[@]}"; do
                    case "$cand" in
                        "$trunc_real"/*|"$runs_parent/$rid_ts"/*) picked=$cand; break ;;
                    esac
                done
            fi
        fi
    fi
    if [ -z "$picked" ]; then
        # No RDIR-local candidate: require unanimous verdict, then newest mtime.
        vs=""
        for cand in "${valid[@]}"; do
            v=$(gui_status_file_verdict "$cand")
            if [ -z "$vs" ]; then
                vs=$v
            elif [ "$v" != "$vs" ]; then
                printf 'gui_harvest: refuse slug=%s conflicting verdicts among %s candidates\n' \
                    "$slug" "${#valid[@]}" >> "${log_path:-/dev/null}" 2>/dev/null || true
                return 0
            fi
        done
        picked=$(ls -t "${valid[@]}" 2>/dev/null | head -n1)
    fi
    [ -n "$picked" ] && [ -f "$picked" ] || return 0

    parent=$(dirname "$picked")
    # Stage every evidence entry before publishing status.txt. A conflicting
    # partial canonical artifact or any copy failure leaves status absent and
    # retains the complete alias for triage; PASS must never outlive its proof.
    local f bn
    for f in "$parent"/*; do
        [ -e "$f" ] || continue
        bn=$(basename "$f")
        case "$bn" in
            status.txt) continue ;;
            .harvested-from) continue ;;
        esac
        if [ -L "$f" ] || { [ -d "$f" ] && [ -n "$(find "$f" -type l -print -quit 2>/dev/null)" ]; }; then
            gui_mark_harvest_invalid "$adir" "$log_path" \
                "evidence contains a symlink: $f"
            return 0
        fi
        if [ -L "$adir/$bn" ] \
                || { [ -d "$adir/$bn" ] && [ -n "$(find "$adir/$bn" -type l -print -quit 2>/dev/null)" ]; }; then
            gui_mark_harvest_invalid "$adir" "$log_path" \
                "canonical evidence contains a symlink: $adir/$bn"
            return 0
        fi
        if [ -e "$adir/$bn" ]; then
            if [ -f "$f" ] && [ -f "$adir/$bn" ]; then
                cmp -s "$f" "$adir/$bn" || {
                    gui_mark_harvest_invalid "$adir" "$log_path" \
                        "evidence conflict; retained source=$f canonical=$adir/$bn"
                    return 0
                }
            elif [ -d "$f" ] && [ -d "$adir/$bn" ]; then
                diff -qr "$f" "$adir/$bn" >/dev/null 2>&1 || {
                    gui_mark_harvest_invalid "$adir" "$log_path" \
                        "evidence directory conflict; retained source=$f canonical=$adir/$bn"
                    return 0
                }
            else
                gui_mark_harvest_invalid "$adir" "$log_path" \
                    "evidence type conflict; retained source=$f canonical=$adir/$bn"
                return 0
            fi
        elif ! cp -a "$f" "$adir/$bn" 2>/dev/null; then
            gui_mark_harvest_invalid "$adir" "$log_path" \
                "evidence copy failed; retained source=$f canonical=$adir/$bn"
            return 0
        fi
    done
    if [ ! -f "$adir/status.txt" ] && ! cp -a "$picked" "$adir/status.txt" 2>/dev/null; then
        gui_mark_harvest_invalid "$adir" "$log_path" \
            "status copy failed; retained source=$picked canonical=$adir/status.txt"
        return 0
    fi
    if ! {
        printf 'harvested_from=%s\n' "$parent"
        printf 'status=%s\n' "$(gui_status_file_verdict "$adir/status.txt")"
        printf 'slug=%s\n' "$slug"
    } > "$adir/.harvested-from" 2>/dev/null; then
        gui_mark_harvest_invalid "$adir" "$log_path" \
            "breadcrumb write failed; retained alias=$parent"
        return 0
    fi
    if [ -n "$log_path" ]; then
        printf '\nqci_gui_harvest: recovered status for slug=%s from %s\n' \
            "$slug" "$parent" >> "$log_path" 2>/dev/null || true
    fi
    if [ "$alias_valid" -eq 1 ] && [ "$parent" = "$alias_real" ]; then
        if ! gui_rebase_artifact_report_links "$adir" "$alias_real"; then
            gui_mark_harvest_invalid "$adir" "$log_path" \
                "report-link rebase failed; retained alias=$alias_dir"
            return 0
        fi
        gui_remove_artifact_alias "$alias_dir" "$adir" || \
            printf 'gui_harvest: retained alias after cleanup refusal: %s\n' \
                "$alias_dir" >> "${log_path:-/dev/null}" 2>/dev/null || true
    fi
    return 0
}

write_agent_prompt() {
    local vm=$1 scenario=$2 prompt=$3 artifact_dir=${4:-} scratch=${5:-} slug=${6:-} rel
    rel=$(gui_scenario_rel "$scenario")
    # Per-attempt artifact dir so a retry's agent writes its status/report to its
    # OWN directory and never clobbers the first attempt's evidence (the audit
    # trail that makes classified retry acceptable). Defaults to the canonical dir.
    # Callers SHOULD pass the short /tmp/qci-gui-art/<hash> directory so weak models
    # cannot truncate a long ci/runs/... path.
    [ -n "$artifact_dir" ] || artifact_dir="$RDIR/gui/$(safe_name "$rel")"
    cat > "$prompt" <<EOF
# qdistro CI GUI scenario runner

Run this scenario against VM \`$vm\` and write a PASS/FAIL/ERROR report.

Scenario file:
\`$scenario\`

## REQUIRED artifact directory (copy this path EXACTLY — do not invent, shorten, or drop path segments)

\`$artifact_dir\`

- Environment: \`QCI_GUI_ARTIFACT_DIR=$artifact_dir\` (already set for this process). Prefer that variable over retyping the path.
- Save screenshots, OCR output, command logs, notes, and click-targets under:
  \`$artifact_dir/\`
- This is a harness-owned real directory. Never move, replace, or resolve it to
  another path; tools including ImageMagick are expected to write there directly.
- In reports, link to sibling evidence with relative paths (for example
  \`step2.png\`), never with the absolute temporary artifact-directory prefix.
- Before returning, write \`$artifact_dir/status.txt\`. Its FIRST word is the
  verdict — exactly one of PASS, FAIL, ERROR, SKIP. For SKIP, put a one-line
  reason on the SAME line after the word (\`SKIP foot is not installed in the
  guest image\`); that reason is what the run report shows, and a bare \`SKIP\`
  makes every skipped scenario read alike. For the other three verdicts write
  the word alone.
- ORDERING (this is the ONLY rule about artifact order, and nothing else in
  this prompt or in the gate contradicts it): write status.txt as soon as you
  have a verdict, before spending any further budget on reports or notes. Every
  other artifact — screenshots, OCR, logs, click-targets — may be written before
  or after it; the gate never compares their timestamps against status.txt.
- Do NOT write evidence under any other \`ci/runs/...\` path. Do NOT drop
  trailing segments from the path above. A truncated path is a harness error.

(Run root for human triage only — NOT for status.txt: \`${RDIR:-}\`)

Rules:
- Read the nearest AGENTS.md before executing the scenario.
- Do not edit source files.
- Every graphical process, dialog, compositor, and input action belongs inside
  the disposable VM named above. Never launch a host GUI program (including
  virt-manager, virt-viewer, remote-viewer, xdg-open, or an app under the host
  DISPLAY/Wayland session). Drive the guest only through the repository's
  vm-exec/vm-gui helpers and virsh. qci deliberately makes the host desktop
  sockets unavailable to this agent process.
- For every model-targeted mouse click, use the two-phase command-line workflow;
  never call raw \`vm-gui click X Y\` or \`xdotool click\` directly:
  1. Activate the target window and run
     \`$QDISTRO_REPO/scripts/vm/vm-gui "\$VMNAME" click-preview X Y "visible target label"\`.
     This moves the real VM pointer to (X,Y) without pressing a button, waits,
     then captures the evidence.
  2. Read BOTH ImageMagick outputs named by the command: the full annotated
     screenshot and the zoomed crop. Confirm that the red ring, crosshair, and
     printed coordinates land on the intended control. When the real cursor is
     visible in the capture, it must align with the ring; cursor invisibility is
     acceptable on renderers that use a hardware cursor plane. If targeting is
     wrong, generate another preview with corrected coordinates. A preview moves
     the pointer but never clicks.
  3. Only after visual confirmation, run
     \`$QDISTRO_REPO/scripts/vm/vm-gui "\$VMNAME" click-confirm <preview-manifest>\`.
     This clicks the exact
     coordinates stored in the reviewed manifest and captures the post-click
     screenshot. All previews, coordinates, timestamps, and confirmations are
     logged automatically under \`$artifact_dir/click-targets/\`.
- Use VMNAME=$vm.
- Scratch files: use isolated per-scenario scratch instead of fixed shared paths
  so parallel runs never collide. On the HOST, write scratch under
  \`\$QCI_SCENARIO_TMPDIR\` (=\`$scratch\`). For GUEST scratch, use the literal
  per-scenario directory \`/tmp/qci-$slug/\` and create it before use. The
  \`\$QCI_SCENARIO_SLUG\` variable is HOST-side only — it is not set inside guest
  shells unless you pass it through yourself (e.g. \`QCI_SCENARIO_SLUG=$slug\`).
  Do NOT write to bare fixed paths like \`/tmp/foo.log\`.
- The agent process starts in a throwaway \`/tmp/qci-agent.XXXXXX\` working
  directory. qci removes it after a successful attempt, preserves it after an
  agent-command failure, and records the path in the agent log. Any tool that
  accidentally writes a relative temporary output stays there instead of
  polluting the source checkout. Required evidence must still be saved under the
  artifact directory above.
- Execute setup, steps, assertions, and cleanup serially, in ONE guest shell
  invocation. Scenario setup helpers commonly arm an \`EXIT\` trap that restores
  the compositor's shell role; if you run Setup in one \`vm-exec\`/\`guest-exec\`
  and the Steps in another, that trap fires the instant Setup's shell exits and
  silently tears down the state your Steps depend on. What then looks like a
  missing precondition is your own teardown.
- Exit code follows the verdict, and the harness is strict about it:
  - PASS — every required assertion passed. Exit 0.
  - SKIP — exit 0, with \`SKIP <reason>\` in status.txt. A SKIP recorded with a
    NONZERO exit is a hard failure, not a skip: a skip artifact left by a
    process that timed out or was killed is not an intentional skip.
  - FAIL — a product-behaviour assertion did not hold. Exit nonzero.
  - ERROR — you could not reach a verdict. Exit nonzero.
- The SCENARIO verdict is decided by the REQUIRED assertions only. A scenario
  whose required assertions all passed is PASS even when one of its own
  OPTIONAL/conditional steps was skipped — a step the scenario itself marks
  "conditional on ...", "skip this step if ...", or "skipped when ...". "Some
  steps skipped, none failed" is PASS, never ERROR. Say which step was skipped
  and why in the report; do not downgrade the verdict for it. ERROR means you
  could not reach a verdict on the REQUIRED assertions — not that the run was
  less than perfectly complete. (qdwin/tests/gui/15-keybinding-events.md in
  full-20260914T194046Z-13620: 1.1/2.1/3.1 observed, only the conditional 4.1
  skipped, recorded ERROR.)
- SKIP is deliberately NARROW. Use it only when a package, binary, service,
  helper, or image capability this scenario requires is verifiably ABSENT here,
  and you can name it and name the check that showed it absent
  (\`command -v foot\`, \`rpm -q ydotool\`, \`systemctl status ...\`). No amount of
  correct driving on your part would make the scenario runnable.
  These are NOT skips — record ERROR (nonzero) instead: your own commands were
  malformed or their state did not survive into a later command; a required
  process started and then died or stopped responding; a command ran but
  returned output you did not expect; anything you simply did not observe while
  the dependency itself was present. Calling one of those a SKIP turns a real
  defect green, which is worse than a red row. When torn between SKIP and
  ERROR, choose ERROR.
- Two concrete examples:
  - Good SKIP: status.txt = \`SKIP foot is not installed in this golden image
    (command -v foot -> not found)\`, exit 0. The dependency is named, the check
    is named, and no driving would have made the scenario runnable.
  - Bad SKIP, record ERROR instead: "the helper client bound the protocol but
    was gone by the time I ran the steps". Something started and then
    disappeared; that is a defect somewhere -- possibly your own driving, see
    the single-guest-shell rule above -- and calling it a skip makes it
    invisible. Exit nonzero with ERROR.
- NEVER assert what is on screen without extracting evidence from the frame.
  A screenshot you captured but did not inspect is not evidence, and "the
  window looked right / the control was missing" written from memory of what
  the scenario said is a fabricated verdict — it is how a passing product is
  reported broken and a broken one reported green. Before ANY visual
  assertion (a label reads X, a button/radio is visible, a pane is empty),
  OPEN THE PNG AND LOOK AT IT. Use your image-viewing tool (\`view_image\` or
  equivalent) on the capture you just took. This is not optional and OCR is
  NOT a substitute for it.
  WHY OCR IS NOT ENOUGH, CONCRETELY. OCR reads text and nothing else. It
  cannot tell you a colour, a layout or geometry, which control has focus,
  what is in front of what, or — most importantly — that something is
  ABSENT. "The pending pane is empty", "no dialog appeared", "the badge is
  gone" are the commonest assertions in these scenarios and OCR cannot
  evidence a single one of them: text it does not find is indistinguishable
  from text it could not read. If you run tesseract and write PASS on an
  absence claim, you have not checked it. You may still run OCR as a
  convenience for reading long text out of a frame you have ALSO looked at;
  it is triage, never the basis of a verdict.
  If you have NO way to open an image, then this scenario's visual assertions
  are UNOBSERVABLE by you: record ERROR (nonzero) naming the missing
  capability. Do NOT record FAIL and do NOT record PASS: with no pixels in
  hand you have no verdict about pixels. Do NOT fall back to OCR and grade
  anyway — that is the failure this paragraph exists to prevent.
  HOW THIS IS ENFORCED — read this carefully, because it is NOT what you leave
  behind, and it is NOT any image file you can produce. The HARNESS records
  every screenshot its own capture tool takes from this VM, and after you exit
  it runs OCR over exactly those captures. Your own OCR files, notes, transcript
  and any image you wrote by other means are NOT graded; they are triage
  material. Three consequences, and they are the whole contract:
    1. CAPTURE THROUGH THE TOOL. A frame counts only when it came from
       \`$QDISTRO_REPO/scripts/vm/vm-gui "\$VMNAME" screenshot ...\`,
       \`... screenshot-fresh ...\`, or a click-preview/click-confirm. A PNG you
       produced any other way is not evidence and cannot make a verdict green.
    2. CAPTURE INTO THE ARTIFACT DIRECTORY, e.g.
       \`$QDISTRO_REPO/scripts/vm/vm-gui "\$VMNAME" screenshot $artifact_dir/s1.png\`
       (click-preview captures already land there). A capture written elsewhere
       is recorded but is not graded unless you copy it in.
    3. NEVER DELETE OR OVERWRITE A CAPTURE. Once the tool has written a frame
       into \`$artifact_dir/\`, removing it or replacing its bytes is detected
       and your verdict is recorded ERROR — including the case where the frame
       showed something you did not like. Keep an unflattering frame and report
       FAIL; that is a correct, valuable result. Hiding it is not.
  If the harness captured nothing from this VM, your PASS or FAIL is recorded
  ERROR no matter what status.txt says. Timestamps are irrelevant — capture
  frames whenever you need them.
- NEVER kill a running \`vm-exec\` and re-issue the same driver. Its periodic
  \`[vm-exec] Waiting... (polls=Ns elapsed=Ns)\` lines mean the TRANSPORT IS
  HEALTHY and your guest command is still running; they are progress, not a
  wedge. vm-exec already enforces its own overall deadline
  (\`QDISTRO_VM_EXEC_TIMEOUT\`, default 1800s) and exits 124 when it fires — but
  that counter is checked BETWEEN steps, not enforced as wall clock, so under
  host pressure the exit can come far later than 1800s. Do NOT wait forever for
  it: put your own cap around the call, \`timeout -k 30 1900 vm-exec ...\`, and
  let that be what ends it. Killing it and re-running leaves the FIRST driver shell alive inside the
  guest, and two drivers then race on one VM: duplicated requests, duplicated
  rows, and no attributable verdict (permissions-gui/45 and /50,
  full-20260914T194046Z-13620). If a command really must be abandoned, send
  vm-exec a signal (on INT/TERM/HUP it attempts an identity-checked
  TERM-then-KILL of the pinned guest tree, and NAMES any descendant it could
  not pin instead of signalling it) rather than
  killing it with SIGKILL, and verify in the guest that nothing from the first
  attempt survived before starting a second one.
- NEVER put a PIPE on vm-exec's stderr in your driver script. Concretely, do
  NOT open your driver with \`exec > >(tee "\$LOG") 2>&1\`, and do not write
  \`out=\$(vm-exec ... 2>&1)\` or \`vm-exec ... 2>&1 | reader\`. This is the
  single commonest way these drivers hang, and it does not look like a hang in
  the script -- it looks like vm-exec being slow.
  WHY. vm-exec puts its own children's fd 1 on an internal capture file, but
  fd 2 is inherited straight through to every virsh/jq descendant it starts.
  Whatever is reading that pipe waits for the pipe to reach EOF, which happens
  when the LAST writer closes it -- NOT when vm-exec exits. One descendant that
  outlives vm-exec holds your driver open for as long as it lives, after the
  guest command is already finished. An outer \`timeout\` on vm-exec does not
  help, because the shell is blocked on a READ, not on the child. Measured with
  a vm-exec leaving a 4s descendant: the \`exec > >(tee …) 2>&1\` driver
  returned after 4.00s, the same driver capturing to a file returned in 0.01s.
  WHAT TO DO INSTEAD. Send both descriptors to a regular FILE and read the file
  afterwards:
      cf=\$(mktemp) || exit 2
      exec {w}>"\$cf" || { rm -f "\$cf"; exit 2; }
      exec {r}<"\$cf" || { exec {w}>&-; rm -f "\$cf"; exit 2; }
      rm -f "\$cf" || { exec {w}>&- {r}<&-; exit 2; }
      rc=0
      "\$QDISTRO_REPO/scripts/vm/vm-exec" "\$VMNAME" 'cmd' \\
          >&"\$w" 2>&"\$w" {w}>&- {r}<&- || rc=\$?
      exec {w}>&-; out=\$(head -c 65536 <&"\$r"); exec {r}<&-
  Check every setup step (an unchecked unlink leaves the capture NAMED while
  the command runs) and collect rc with \`|| rc=\$?\` (under \`set -e\` a bare
  call aborts before you can read it).
  If you only want a log of your whole run, redirect to a FILE
  (\`exec >"\$QCI_GUI_ARTIFACT_DIR/driver.log" 2>&1\`) -- a file has no reader
  to wait on. Use
  \`tee\` only where no vm-exec call is in scope.
- Guest logs and scratch must be per-scenario and must not assume a clean /tmp.
  ANY fixed shared guest path (\`/tmp/<something>.log\`) can already exist
  ROOT-owned from the golden image, and a non-root writer then dies with
  \`Permission denied\` and produces a black screenshot that looks like a product
  failure (permissions-gui/50, full-20260914T194046Z-13620). Redirect YOUR OWN
  logs to \`/tmp/qci-$slug/<name>.log\`. Do NOT invent a shipped log path to
  clear: the admin launchers write under
  \`\${XDG_STATE_HOME:-/home/admin/.local/state}/qdistro/\` (admin-app.log,
  qterminal-tui.log), which is per-user and not a shared /tmp path — read it for
  diagnostics, never delete it as root.
- Diagnose your OWN tooling before blaming the product:
  - First confirm your setup/driver commands actually executed. A shell
    parser/usage error from one of your own commands (e.g. \`option requires an
    argument\`, \`unexpected EOF\`, \`syntax error near\`) is a tooling error on
    your side — fix and re-issue the command; do NOT record a product FAIL on
    that basis.
  - Treat an empty IPC response as inconclusive until you retry it or a nonzero
    command result explains it — not as proof the compositor is broken.
  - If you cannot get your own driver commands to run, record ERROR, not FAIL.
  - Only attribute a failure to qdwin/Wayland/libweston after an IPC call
    returned a valid response or a failure code proving it reached that layer.

Start by reading:
- \`$scenario\`
- \`$(dirname "$scenario")/AGENTS.md\` if present, otherwise the closest parent AGENTS.md.
EOF
    # Optional verbose-debug appendix (QCI_GUI_DEBUG=1). Triage aid: have the
    # agent capture the exact command/stderr at the point of any failure and a
    # precise root-cause verdict, so we can tell agent-weakness from a real
    # test/product defect. Off by default — never affects normal grading prompts.
    if [ "${QCI_GUI_DEBUG:-0}" = 1 ]; then
        cat >> "$prompt" <<EOF

## DEBUG MODE (verbose triage — this run only)

Produce a CONCISE debug log at \`$artifact_dir/debug.md\` IN ADDITION to
status.txt, under the single ORDERING rule stated in the artifact rules above —
never let debugging consume your whole budget and leave no verdict.
Keep it focused (do NOT paste full output of every command — that is too slow):
- At the FIRST point anything goes wrong, capture just that: the exact failing
  command, its rc, the relevant stderr/journal lines, and one screenshot. Label
  it "FAILURE POINT". One failure point is enough; do not keep probing forever.
- Classify the failure into ONE of: (a) AGENT-TOOLING — your own command was
  malformed/quoted wrong; (b) PRECONDITION — a required app/service/image/env is
  missing on this VM (SKIP/ERROR, not a product bug); (c) STALE-ASSERTION — the
  scenario asserts an outdated name/value but the product behaves correctly;
  (d) TEST-RACE — a timing/ordering bug in the scenario, not the product;
  (e) PRODUCT-DEFECT — a genuine qdwin/qdshell/qdlocker bug. One sentence of
  evidence for the choice.
- If a weaker model previously failed this scenario, state in one line whether
  YOU got past that step and what you did differently.
EOF
    fi
}

agent_artifact_status() {
    local artifact_dir=$1 log_path=$2 raw=""
    if [ -f "$artifact_dir/.harvest-invalid" ]; then
        printf 'UNKNOWN\n'
        return 0
    fi
    if [ -f "$artifact_dir/status.txt" ] && [ ! -L "$artifact_dir/status.txt" ]; then
        raw=$(tr -d '\r' < "$artifact_dir/status.txt" | awk 'NF {print toupper($1); exit}')
        # Some small-model runs wrote a literal trailing "n" instead of a
        # newline (`PASSn`). Treat only that exact typo as the intended verdict;
        # arbitrary words like PASSING still fail closed as UNKNOWN below.
        case "$raw" in
            PASSN) raw=PASS ;;
            FAILN) raw=FAIL ;;
            ERRORN) raw=ERROR ;;
            SKIPN) raw=SKIP ;;
        esac
    elif [ -f "$artifact_dir/report.md" ] && [ ! -L "$artifact_dir/report.md" ]; then
        raw=$(awk '
            NR > 30 { exit }
            /(^# .*(FAIL|ERROR|SKIP|PASS))|([[:space:]]-[[:space:]](FAIL|ERROR|SKIP|PASS))|([—-][[:space:]]*(FAIL|ERROR|SKIP|PASS)[[:space:]]*$)/ {
                line=toupper($0)
                if (line ~ /ERROR/) { print "ERROR"; exit }
                if (line ~ /FAIL/) { print "FAIL"; exit }
                if (line ~ /SKIP/) { print "SKIP"; exit }
                if (line ~ /PASS/) { print "PASS"; exit }
            }
        ' "$artifact_dir/report.md")
    fi
    case "$raw" in
        PASS|FAIL|ERROR|SKIP) printf '%s\n' "$raw" ;;
        *) printf 'UNKNOWN\n' ;;
    esac
}

# ---------------------------------------------------------------------------
# VISUAL-EVIDENCE CONTRACT (harness-attested)
#
# The GUI agent is an UNTRUSTED component: it authors status.txt and the gate
# grades it. Round 2 tried to close this by requiring the agent to LEAVE an
# evidence artifact (an OCR TSV, or a structured image-open event in its log).
# An adversarial review reproduced the obvious bypass: the agent has arbitrary
# host shell, so it authors the evidence too. A single line
# `{"message":"view_image /tmp/unrelated.png"}` appended to its own log turned
# an invented PASS green. Requiring the tested component to file its own
# evidence is not a defense.
#
# So the observation moved to the harness. After the agent exits and its
# artifacts are harvested, the GATE ITSELF runs OCR over the frames of THIS
# scenario and stores the result in a harness-owned `visual-evidence/`
# subdirectory it creates at that moment.
#
# Round 3 stopped there, and round 4's review reproduced the residual bypass in
# three moves:
#
#   1. the agent pre-creates `visual-evidence/`; the gate renames it to
#      `visual-evidence.agent-supplied`, but the frame finder excluded only
#      `visual-evidence/*`, so images planted in it became OCR INPUT;
#   2. any hand-written blank/unrelated PNG anywhere under the artifact dir was
#      a "frame", and a successful OCR run that read ZERO words returned `ok`,
#      so one blank image blessed an arbitrary visual PASS;
#   3. cheaper still: capture honestly, then DELETE the damning frame and keep
#      a clean one. Nothing knew a frame had ever existed.
#
# All three share one root cause: the frame SET was whatever the agent left on
# disk. So the set is no longer derived from the disk. The capture tools go
# through a shared library that TAKES the screenshot and appends a row to a
# harness-owned capture ledger for the frame it just wrote -- sequence
# number, UTC time, VM, the
# destination path, byte count, sha256, and a hash chain over all preceding
# rows. The gate creates that log before the agent starts (outside the artifact
# directory, in the run tree) and reads it after the agent exits. The graded
# frame set is the intersection of "the harness captured this" and "this is
# still on disk in the artifact dir", matched BY DIGEST so an honest
# capture-then-copy still counts.
#
# THE TRUST MODEL, DECIDED BY THE OWNER 2026-09-16, because everything below
# only makes sense against it:
#
#   The scenario driver is TRUSTED. It is a capable model and it is assumed to
#   be honest. This ledger is NOT an adversarial boundary and must never be
#   described as one. It exists to (a) make accidents and flakes impossible to
#   mistake for verdicts, and (b) leave a record good enough to DIAGNOSE a bad
#   verdict afterwards -- including one produced by a driver that lied.
#
#   That is a deliberate trade. An adversarial boundary at this uid IS
#   buildable (see "WHAT THIS DOES NOT PROVE"); it was not built, because the
#   failures this project has actually suffered were honest drivers taking bad
#   shortcuts and flaky captures, not forgery, and a forensic record answers
#   those faster than a barrier would prevent them. If forgery ever becomes a
#   real concern, that decision gets revisited -- it is a decision, not a limit.
#
# The contract is therefore, stated at exactly its real strength:
#
#     "the graded frames are the ones this scenario's capture ledger records
#      for the VM this scenario was assigned; that ledger has not been edited
#      since the gate froze it; and the harness read those frames itself"
#
# It is NOT, and must never be written as, "these frames came out of virsh
# screenshot". Round 6 was right that the earlier wording claimed that and the
# code could not deliver it. See the ledger's own block further down for the
# full boundary argument; the summary is:
#
# WHAT THIS PROVES
#   * A PNG the agent merely authored and left on disk has no capture row and
#     is NOT evidence; a scenario whose ledger records no capture is ERROR, no
#     matter what images it leaves behind.
#   * Every graded row names the VM the GATE assigned. The producer refuses a
#     row for any other VM and the verifier rejects a ledger containing one, so
#     at QCI_JOBS=8 a neighbour's healthy VM cannot supply this scenario's
#     evidence.
#   * Both virsh lanes capture THROUGH the library, which attests only what it
#     just wrote, so nothing chooses those bytes. The in-guest qdwin lane's
#     capture is a guest protocol the library cannot run, so it hands its file
#     over via capture_attest_frame -- which DOES take caller-supplied bytes,
#     gated only on the bound VM. That is a documented convenience for the
#     capture tools, not a boundary, and an earlier version of this bullet
#     claiming no such call exists was simply wrong (B round 1, both
#     reviewers). There is no command-line subcommand for it.
#   * Deleting a capture that was taken INTO the artifact tree is detected
#     (its row survives, its bytes do not) and is ERROR. Deleting the row too
#     breaks the hash chain, which is also ERROR.
#   * After the agent exits the ledger is FROZEN by a gate seal whose chain
#     head lives only in the gate's process memory, so tail truncation, seal
#     removal and whole-ledger reconstruction are all ERROR.
#   * "OCR read zero words" is no longer a bypass, because words are not what
#     makes a frame evidence -- attested capture is. A genuinely blank attested
#     frame is a real observation of a blank screen and stays acceptable.
#
# WHAT THIS DOES NOT PROVE (do not let the note imply otherwise):
#   * A ROW IS NOT PROOF OF A CAPTURE. The agent runs as the same host uid as
#     the gate, so an agent that re-implements the published chain format can
#     append a row for bytes it invented, and nothing here can tell that row
#     from a real capture.
#     An earlier version of this comment said "no in-process design closes it".
#     That was FALSE and is corrected here: a pre-agent supervisor can set
#     PR_SET_DUMPABLE=0, keep a random key in a sealed memfd across a
#     close-on-exec agent launch, and MAC every row; the agent cannot recover
#     that key through /proc, ptrace, process_vm_readv or pidfd_getfd without
#     CAP_SYS_PTRACE. So this is NOT a limit of the uid -- it is a boundary
#     this harness chose not to build (see THE TRUST MODEL above). Saying
#     "impossible" when the honest word is "declined" is exactly the
#     over-claiming this file has been repeatedly corrected for.
#     What the ledger does supply is that the no-effort routes are closed -- a
#     neighbour's VM, an edited or truncated ledger, a PNG the agent merely
#     authored -- so passively writing PASS without looking leaves no evidence
#     at all. "Every route" overstated it: calling capture_attest_frame is not
#     no-effort, but it is not hard either, and it is not closed.
#   * IT CANNOT COMPEL A CAPTURE. An agent that never screenshots the failing
#     moment, or that drops its own last row before exiting, look identical
#     from here. `<!-- qci:visual-captures: N -->` narrows it and IS ACTIVE:
#     gui_scenario_min_captures reads it and a shortfall in ledger rows is
#     ERROR (an earlier version of this bullet said DISABLED, which was simply
#     false -- see gui_visual_evidence_status). It is WEAKER than a count of
#     usable frames, because ledger rows include helper retries that were
#     rejected and never became evidence, so a scenario declaring 2 can satisfy
#     the floor with 1 real frame plus 1 rejected attempt. Building correct
#     counting semantics was judged not worth it under the trust model above;
#     no scenario declares a count today, so the floor is 1 everywhere.
#   * IT DOES NOT PROVE THE DRIVER LOOKED. A frame can be captured, attested,
#     sealed and never opened. gui_count_image_opens records a DIAGNOSTIC on
#     every attempt, first and retried -- it is the first thing to read when a
#     visual verdict looks wrong -- but it is not a gate, it does not change a
#     verdict, and it counts MENTIONS rather than tool calls. A zero is a
#     trend signal worth reading first, not proof the driver did not look. See
#     the function for why.
#   * It does not adjudicate the ASSERTION. OCR reads text. It cannot establish
#     a colour, a layout/geometry claim, focus, z-order, animation, or the
#     ABSENCE of a control. For those the contract proves attested observation
#     plus the structural facts the harness can compute itself (how many
#     captures, how many DISTINCT frames), and says so in the note.
#   * It does not link evidence to individual assertions. Linkage is per-FRAME
#     (every attested frame of this scenario is OCR'd and recorded), not
#     per-assertion.
#
# SKIP/UNKNOWN are untouched (they make no pixel claim) and `qci:visual: none`
# scenarios are untouched. There is deliberately NO env bypass.
# ---------------------------------------------------------------------------

# Name of the harness-owned evidence subdirectory inside each artifact dir.
GUI_VISUAL_EVIDENCE_DIR=visual-evidence

# Probe the host OCR backend. Presence is NOT sufficient: require the real
# banner (`tesseract 5.5.3` on the first line), because a "backend present"
# claim built on `command -v` alone breaks on any packaging accident that puts
# something else under the name.
#
# On THIS host the name is not actually contested: /usr/bin/tesseract belongs to
# tesseract-ocr 5.5.3, and the unrelated game ships /usr/bin/tesseract-game,
# whose first --version line is `init: sdl`. Earlier comments here and in
# doc/dev.md claimed the game took the `tesseract` name; it does not (sol and
# fable, B round 2). The banner check stays because a PATH is not ours to
# assume, not because of that story. Any numeric version is accepted.
# Echoes "tesseract <version>" and returns 0 when a real OCR binary is present;
# echoes nothing and returns 1 otherwise.
gui_ocr_backend_probe() {
    local bin=${QCI_OCR_BIN:-tesseract} first ver
    command -v "$bin" >/dev/null 2>&1 || return 1
    first=$("$bin" --version 2>&1 | head -1)
    ver=$(printf '%s' "$first" | sed -nE 's/^tesseract[[:space:]]+v?([0-9][0-9.]*).*/\1/p')
    [ -n "$ver" ] || return 1
    printf 'tesseract %s\n' "$ver"
}

# Preflight observation line(s) for the visual-evidence backend. Pure w.r.t. the
# run tree (probes the host only), so it is reported in gui/preflight.txt next to
# the other shared-capability gaps. Args: ocr_desc (from gui_ocr_backend_probe).
gui_visual_backend_observation() {
    local ocr=${1:-}
    if [ -n "$ocr" ]; then
        printf 'text corroboration: OCR %s (host, run BY THE GATE over attested frames; recorded, never verdict-affecting)\n' "$ocr"
    else
        printf '%s\n' "text corroboration ABSENT: nothing on the host answers with a real tesseract version banner. Visual scenarios still grade -- the verdict rests on the sealed VM-bound ledger and a vision-capable runner, not on OCR -- but the per-frame text column will read 'skip'. Optional: sudo zypper -n install tesseract-ocr tesseract-ocr-traineddata-english"
    fi
}

# Does this scenario make pixel-dependent assertions?
#
# The scenario DECLARES this and the declaration is MANDATORY. Round 2 fell back
# to grepping the scenario text for `screenshot`/`.png` when no marker was
# present; that fallback was case-sensitive, missed any other capture helper or
# phrasing ("compare the rendered frame"), and silently classified such a
# scenario `none` — i.e. exempted it from the contract. It also failed OPEN on a
# file carrying BOTH markers. There is now no fallback: an undeclared, unknown,
# or conflicting declaration is an ERROR the author must resolve, and
# gui_validate_scenarios rejects it before any golden/VM/agent work.
#
# The marker, anywhere in the file (spacing tolerant, value case-insensitive):
#     <!-- qci:visual: required -->   a required assertion is decided by pixels
#     <!-- qci:visual: none -->       no required assertion is decided by pixels
#                                     (capturing frames for diagnostics is fine)
#
# Echoes `required`, `none`, or `invalid:<why>`. Arg: scenario file path.
gui_scenario_visual_mode() {
    local file=$1 toks n
    if [ ! -f "$file" ] || [ ! -r "$file" ]; then
        printf 'invalid:scenario file is not a readable file\n'
        return 0
    fi
    toks=$(grep -oiE '<!--[[:space:]]*qci:visual[[:space:]]*[:=][[:space:]]*[A-Za-z]+[[:space:]]*-->' "$file" 2>/dev/null \
        | sed -E 's/^.*[:=][[:space:]]*([A-Za-z]+)[[:space:]]*-->$/\1/' \
        | tr '[:upper:]' '[:lower:]' | sort -u)
    n=$(printf '%s' "$toks" | grep -c . || true)
    if [ "${n:-0}" -eq 0 ]; then
        printf 'invalid:no <!-- qci:visual: required|none --> declaration\n'
        return 0
    fi
    if [ "${n:-0}" -gt 1 ]; then
        printf 'invalid:conflicting qci:visual declarations (%s)\n' "$(printf '%s' "$toks" | tr '\n' ',' | sed 's/,$//')"
        return 0
    fi
    case "$toks" in
        required) printf 'required\n' ;;
        none) printf 'none\n' ;;
        *) printf 'invalid:unknown qci:visual value %s (expected required or none)\n' "$toks" ;;
    esac
}

# Every frame THIS scenario captured, in the harvested artifact dir. Per-frame
# linkage lives here: the set is derived from the scenario's own artifact
# directory, so a neighbour's screenshots can never satisfy this scenario, and
# there is no path by which "some image somewhere in the tree" counts.
# Symlinks are excluded (an agent must not point the OCR at a file outside its
# artifact dir).
#
# EVERY quarantined tree is excluded, not just the live output directory. Round
# 3 excluded `visual-evidence/*` only, while gui_visual_evidence_status renames
# a pre-planted directory to `visual-evidence.agent-supplied` -- so images
# planted inside it came back as OCR INPUT one rename later. The exclusion is
# now by directory NAME PREFIX at any depth (`-prune` on `visual-evidence*`),
# which covers the live dir, the `.agent-supplied` quarantine, any future
# quarantine suffix, and a nested copy of either.
#
# Args: artifact_dir. Echoes paths, sorted.
gui_visual_frames() {
    local adir=$1
    [ -d "$adir" ] || return 0
    # Depth 6, not 4: a frame nested deeper than the search would be attested
    # but invisible on disk, which the reconciliation below would (wrongly) read
    # as omission. Depth costs nothing now that attestation, not presence on
    # disk, decides what counts as evidence.
    find "$adir" -maxdepth 6 \
        \( -name "$GUI_VISUAL_EVIDENCE_DIR" -o -name "$GUI_VISUAL_EVIDENCE_DIR.*" \) -prune -o \
        -type f \
        \( -iname '*.png' -o -iname '*.ppm' -o -iname '*.pnm' \
           -o -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.webp' -o -iname '*.tif' -o -iname '*.tiff' \) \
        -print 2>/dev/null | LC_ALL=C sort
}

# ---------------------------------------------------------------------------
# THE HARNESS CAPTURE LOG
#
# Written by every harness CAPTURE TOOL -- one row per frame it captures -- via
# the shared scripts/vm/lib/capture-attest.sh:
#     qdistro/scripts/vm/vm-gui            (virsh; labwc/admin lane)
#     qdwin/tests/gui/qdwin-helpers.sh     (in-guest qdshell capture; qdwin lane)
#     qdwin/tests/apps/qdwin-apps-helpers.sh (virsh; qdwin apps lane)
# and read by the gate. It lives in the RUN TREE, never in the artifact directory: the
# artifact directory is the agent's workspace and is harvested/copied wholesale,
# and an evidence ledger that travels inside the thing it vouches for is not a
# ledger. The prompt never names it. It IS reachable: the capture tools find it
# through QCI_GUI_CAPTURE_LOG in the environment, which `env` also shows the
# agent -- same uid means it could not have been hidden anyway. Its value is
# that it is not part of the agent's workspace and not something an agent
# writing an unevidenced PASS ever touches.
#
# Format (TSV, tab-separated, no embedded tabs -- the writer strips them):
#   line 1   #qci-capture-log<TAB>2<TAB><seed><TAB><vm>
#   line 2   seq  ts_utc  vm  scope  bytes  sha256  path  chain
#   line 3+  data rows, seq counting 1..N with no gaps
#
# `scope` is `in-tree` when the capture's destination was inside the artifact
# directory the harness handed the agent, `out-of-tree` otherwise (e.g. the
# vm-gui default /tmp/vm-screenshot.png, which many scenarios then copy in).
# The distinction matters for omission detection: an in-tree capture was written
# where evidence is collected, so its later absence is omission; an out-of-tree
# capture may legitimately never be harvested, so its absence is reported, not
# punished.
#
# `rejected` marks a candidate the CAPTURE TOOL itself judged unusable -- blank,
# stale, the wrong window. It is written so the ledger stays a complete account
# of what the harness took, and it counts NOWHERE: not as a frame, not toward a
# declared floor, and never as evidence. A frame the harness already refused
# must not be able to satisfy the contract on its own.
#
# `chain` is sha256("<prev-chain>\t<seq>\t<ts>\t<vm>\t<scope>\t<bytes>\t<sha256>\t<path>"),
# chained from the seed. It makes an INTERIOR deleted row, and any reordering,
# detectable without reading anything the agent left in the artifact dir.
#
# ===========================================================================
# WHAT THIS CONTRACT PROVES — and, exactly, what it does not.
#
# THE TRUST BOUNDARY. The scenario agent runs as the SAME HOST UID as this
# gate. That is decisive, and it is worth stating plainly rather than papering
# over: at the same uid there is no secret, no unwritable file, no
# unimpersonable producer and no authenticable IPC peer. Any key the producer
# could read, the agent reads; any function the producer exposes, the agent
# calls; any socket the producer connects to, the agent connects to;
# SO_PEERCRED on that socket returns the agent's own uid. Therefore:
#
#   NO IN-PROCESS SCHEME AT THIS UID CAN MAKE AN ATTESTED ROW UNFORGEABLE.
#
# An agent that re-implements the (published, deliberately simple) chain
# format can append a row for bytes it invented, and nothing in this file can
# distinguish that row from a real capture. Only privilege separation — a
# distinct uid or user namespace for the agent, so the ledger and the capture
# tools are not writable/inheritable by it — makes that boundary real. The
# costed proposal for that is recorded in
# qdistro/../todo/reviews/qci-r7-attestation-boundary.md.
#
# GIVEN THAT, HERE IS THE EXACT CLAIM.
#
# PREVENTED (not merely detected):
#   * Cross-VM evidence through the provided tools. The ledger header names
#     the VM the gate assigned; scripts/vm/lib/capture-attest.sh REFUSES to
#     append a row for any other VM, and this verifier rejects a ledger whose
#     header or any of whose rows names a VM other than the assigned one. With
#     QCI_JOBS=8, screenshotting a neighbour's healthy VM cannot produce
#     evidence for this scenario.
#   * Blessing arbitrary bytes through a GENERIC entry point. Both virsh lanes
#     now capture THROUGH the library (capture_virsh_screenshot), which
#     attests only the file it itself just wrote; there is no
#     `vm-gui <vm> attest <file>` and no exported `capture_attest <file>`.
#     One gated helper (capture_attest_frame) remains for the in-guest qdwin
#     lane, whose capture is a guest protocol that cannot live in the library;
#     it is bound to the assigned VM, and it is documented as a convenience,
#     NOT as a boundary.
#
# DETECTED (tamper-evident):
#   * Interior deletion, reordering, substitution, re-seeding, a rewritten
#     header, and ANY edit at all after the agent exits — including TAIL
#     TRUNCATION. After the agent has exited, the gate appends a SEAL row
#     carrying a nonce it generated in its own process memory, and keeps the
#     resulting chain head and row count IN MEMORY ONLY, never on disk. A
#     later edit cannot reproduce that head without a sha256 preimage.
#   * A harness capture written INTO the artifact dir and then removed
#     (reconciliation by digest).
#
# NEITHER PREVENTED NOR DETECTED — stated so nothing here reads as proof:
#   * FABRICATION DURING THE RUN. An agent that re-implements the chain can
#     append a row for a blank or unrelated image before it exits. No
#     IN-PROCESS scheme can close it (see the boundary note above); the
#     out-of-process supervisor that could was DECLINED, not ruled impossible.
#   * DECLINING TO CAPTURE THE FAILING MOMENT. This is the honest form of the
#     "tail truncation" attack, and it deserves to be named precisely: an
#     agent deleting its own last row BEFORE it exits is indistinguishable
#     from an agent that simply never took that screenshot. No ledger
#     mechanism — append-only fd, unlinked file, or otherwise — can tell those
#     apart, because the agent chooses the capture moments. The only thing
#     that closes it is the SCENARIO declaring how many frames must exist:
#     `<!-- qci:visual-captures: N -->` is honoured here (see
#     gui_scenario_min_captures) and a shortfall is ERROR. Scenarios that do
#     not declare one get the floor of 1.
#   * THE TRUTH OF THE AGENT'S VERDICT. OCR evidences text only. Colour,
#     geometry, focus, z-order, animation and absence are attested as
#     OBSERVED, never adjudicated.
# ===========================================================================
# ---------------------------------------------------------------------------

GUI_CAPTURE_LOG_MAGIC='#qci-capture-log'
GUI_CAPTURE_LOG_VERSION=2

# Create an empty capture log for one scenario attempt, BOUND to the VM the
# gate assigned to it. The bound VM is what makes a neighbour worker's capture
# inadmissible here; a ledger with no bound VM is refused rather than
# provisioned, so the binding cannot be lost by omission.
# Args: path vm. Returns 0 on success (the path is then safe to hand to vm-gui).
gui_capture_log_init() {
    local path=$1 vm=${2:-} seed
    case "$vm" in
        ''|*[$'\t\n\r']*) return 1 ;;
    esac
    mkdir -p "$(dirname "$path")" 2>/dev/null || return 1
    # Never append to, or follow, something that is already there.
    rm -rf -- "$path" 2>/dev/null || true
    seed=$( { head -c 32 /dev/urandom 2>/dev/null || date +%s%N; } | sha256sum | awk '{print $1}')
    {
        printf '%s\t%s\t%s\t%s\n' "$GUI_CAPTURE_LOG_MAGIC" "$GUI_CAPTURE_LOG_VERSION" "$seed" "$vm"
        printf 'seq\tts_utc\tvm\tscope\tbytes\tsha256\tpath\tchain\n'
    } > "$path" || return 1
}

# FREEZE the ledger. Called by the gate ONCE, AFTER the scenario agent has
# exited and before anything is graded.
#
# This is the whole tail-truncation answer, and it is worth being precise about
# what it buys. It appends a final `seal` row whose payload contains a nonce
# generated HERE, in the gate's own process, and echoes the resulting row count
# and chain head. The caller keeps both in shell variables and passes them to
# gui_capture_log_verify; they are never written anywhere the agent can read,
# and they are established at a moment when the agent is no longer running.
# Consequently every edit to the ledger after the agent exits — truncating the
# tail, dropping the seal, rebuilding the whole chain from the seed — produces
# a different head and is detected. What it does NOT buy is protection against
# the agent deleting its own last row BEFORE it exits, which is the same act as
# never taking that screenshot; see the contract block above.
#
# Echoes "<total_rows>\t<chain_head>". Args: path vm. Returns 1 on failure,
# which the caller must treat as fail-closed (an unsealed ledger is not graded).
gui_capture_log_seal() {
    local path=${1:-} vm=${2:-} nonce rows prev ts payload chain
    [ -n "$path" ] || return 1
    [ ! -L "$path" ] && [ -f "$path" ] || return 1
    [ -n "$vm" ] || return 1
    nonce=$( { head -c 32 /dev/urandom 2>/dev/null || date +%s%N; } | sha256sum | awk '{print $1}')
    [ -n "$nonce" ] || return 1
    rows=$(tail -n +3 -- "$path" 2>/dev/null | grep -c . || true)
    [ -n "$rows" ] || rows=0
    rows=$((rows + 1))
    if [ "$rows" -eq 1 ]; then
        prev=$(head -1 -- "$path" | awk -F'\t' '{print $3}')
    else
        prev=$(tail -1 -- "$path" | awk -F'\t' '{print $8}')
    fi
    [ -n "$prev" ] || return 1
    ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    payload=$(printf '%s\t%s\t%s\tseal\t0\t%s\t(gate-seal)' "$rows" "$ts" "$vm" "$nonce")
    chain=$(printf '%s\t%s' "$prev" "$payload" | sha256sum | awk '{print $1}')
    printf '%s\t%s\n' "$payload" "$chain" >> "$path" || return 1
    printf '%s\t%s\n' "$rows" "$chain"
}

# The scenario's declared minimum number of harness captures, from
# `<!-- qci:visual-captures: N -->`. Defaults to 1 (at least one capture), and
# an unparseable/zero declaration also floors at 1. This is the ONLY mechanism
# that can close frame omission at this uid: an agent that declines to capture
# the failing moment is otherwise indistinguishable from one that captured it
# and deleted the row before exiting. Args: scenario_file.
gui_scenario_min_captures() {
    local n
    n=$(grep -aoiE '<!--[[:space:]]*qci:visual-captures[[:space:]]*[:=][[:space:]]*[0-9]+[[:space:]]*-->' "${1:-}" 2>/dev/null \
        | head -1 | grep -oE '[0-9]+' | head -1)
    case "$n" in
        ''|0|*[!0-9]*) printf '1\n' ;;
        *) printf '%s\n' "$n" ;;
    esac
}

# Verify a capture log's shape, VM binding and hash chain, and echo one summary
#   rows=N in_tree=I out_tree=O
# where N counts CAPTURE rows only (the gate's own seal row is excluded).
# Returns 0 when the log is present and internally consistent, 1 otherwise
# (echoing `bad:<why>`).
#
# Args: path [expected_vm] [expected_total_rows] [expected_chain_head]
# The last three are the gate's IN-MEMORY anchor from gui_capture_log_seal plus
# the VM it assigned. When supplied (the gate always supplies them) the ledger
# must match them exactly, which is what freezes it after the agent exits.
gui_capture_log_verify() {
    local path=$1 want_vm=${2:-} want_rows=${3:-} want_head=${4:-}
    local seed bound prev line seq ts vm scope bytes sum fpath chain want
    local n=0 total=0 in_tree=0 out_tree=0 seals=0
    if [ -z "$path" ]; then
        printf 'bad:no capture log was provisioned for this scenario\n'; return 1
    fi
    if [ -L "$path" ] || [ ! -f "$path" ]; then
        printf 'bad:the harness capture log is missing (%s)\n' "${path##*/}"; return 1
    fi
    seed=$(head -1 "$path" | awk -F'\t' -v m="$GUI_CAPTURE_LOG_MAGIC" \
        -v v="$GUI_CAPTURE_LOG_VERSION" '$1 == m && $2 == v { print $3 }')
    bound=$(head -1 "$path" | awk -F'\t' -v m="$GUI_CAPTURE_LOG_MAGIC" \
        -v v="$GUI_CAPTURE_LOG_VERSION" '$1 == m && $2 == v { print $4 }')
    if [ -z "$seed" ] || [ -z "$bound" ]; then
        printf 'bad:the harness capture log header is missing or was rewritten\n'; return 1
    fi
    # VM BINDING, first half: the ledger must be bound to the VM the GATE
    # assigned, which it knows from its own memory and not from this file.
    if [ -n "$want_vm" ] && [ "$bound" != "$want_vm" ]; then
        printf 'bad:the capture ledger is bound to VM %s but this scenario was assigned %s\n' \
            "$bound" "$want_vm"; return 1
    fi
    prev=$seed
    while IFS= read -r line; do
        total=$((total + 1))
        IFS=$'\t' read -r seq ts vm scope bytes sum fpath chain <<<"$line"
        if [ "$seq" != "$total" ]; then
            printf 'bad:capture-log row %d is out of sequence (seq=%s) — rows were removed or reordered\n' "$total" "${seq:-empty}"
            return 1
        fi
        # VM BINDING, second half: no row may name another VM. The producer
        # already refuses these; this catches a hand-appended row.
        if [ "$vm" != "$bound" ]; then
            printf 'bad:capture-log row %d records VM %s, not this scenario'"'"'s VM %s — a capture of another VM is not evidence here\n' \
                "$total" "${vm:-empty}" "$bound"
            return 1
        fi
        want=$(printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s' \
            "$prev" "$seq" "$ts" "$vm" "$scope" "$bytes" "$sum" "$fpath" | sha256sum | awk '{print $1}')
        if [ "$chain" != "$want" ]; then
            printf 'bad:capture-log row %d fails its hash chain — the capture ledger was edited\n' "$total"
            return 1
        fi
        prev=$chain
        case "$scope" in
            seal) seals=$((seals + 1)) ;;
            in-tree) n=$((n + 1)); in_tree=$((in_tree + 1)) ;;
            *) n=$((n + 1)); out_tree=$((out_tree + 1)) ;;
        esac
    done < <(tail -n +3 -- "$path" 2>/dev/null)
    # THE SEAL ANCHOR. Both values were produced in the gate's own process after
    # the agent exited and were never written anywhere the agent can read, so a
    # mismatch means the ledger changed after it was frozen — tail truncation
    # included.
    if [ -n "$want_rows" ]; then
        if [ "$total" != "$want_rows" ]; then
            printf 'bad:the capture ledger has %d row(s) but the gate sealed it at %s — rows were added or removed after the agent exited\n' \
                "$total" "$want_rows"; return 1
        fi
        if [ "$seals" -ne 1 ]; then
            printf 'bad:the capture ledger carries %d gate seal row(s), expected exactly 1\n' "$seals"; return 1
        fi
    fi
    if [ -n "$want_head" ] && [ "$prev" != "$want_head" ]; then
        printf 'bad:the capture ledger does not end at the chain head the gate sealed — it was rewritten after the agent exited\n'
        return 1
    fi
    printf 'rows=%d in_tree=%d out_tree=%d\n' "$n" "$in_tree" "$out_tree"
}

# Echo the data rows of a capture log (seq..chain), unverified. Args: path.
gui_capture_log_rows() {
    [ -f "${1:-}" ] || return 0
    tail -n +3 -- "$1" 2>/dev/null
}

# THE OBSERVATION. Run the host OCR backend over every frame of this scenario
# and record what it found, under a directory the gate creates AFTER the agent
# has exited. Nothing the agent wrote is read as evidence; only the frames'
# pixels are.
#
# Writes <outdir>/manifest.tsv (one row per frame: sha256, bytes, relative
# frame path, tsv name, ocr rc, word count), <outdir>/<frame>.tsv per frame, and
# <outdir>/ocr.log (backend stderr). Echoes a single summary line
# `frames=N ocr_ok=K ocr_fail=J text_frames=T words=W`.
# Returns 0 when at least one OCR invocation succeeded, 1 otherwise.
# Args: artifact_dir outdir [frame_list_file]
#
# frame_list_file, when given, is the EXACT set of frames to read (one absolute
# path per line) -- that is how the contract restricts OCR to harness-attested
# captures. Without it every image under the artifact dir is read, which is only
# appropriate for the standalone/diagnostic use.
# Can this file be decoded as an image AT ALL? This is deliberately independent
# of OCR. While OCR was a grading PRECONDITION a corrupt file failed the OCR gate
# and was noticed by accident; round 9 demoted OCR, which removed that accident,
# and an external review then produced `ok:` + exit 0 for a file containing the
# twelve bytes `not an image` pushed through the real producer and the real seal.
# A frame the gate cannot decode is not evidence of anything and must be VISIBLE
# as such in the record, whatever OCR does or does not say about it.
# Echoes yes|no|unknown. `unknown` when no decoder is installed -- "cannot tell"
# is never recorded as "fine".
gui_frame_is_decodable() {
    local f=$1 dims
    command -v magick >/dev/null 2>&1 || { printf 'unknown\n'; return 0; }
    dims=$(magick identify -quiet -format '%w %h' "$f" 2>/dev/null) || { printf 'no\n'; return 0; }
    case "$dims" in ''|*' 0'|'0 '*) printf 'no\n'; return 0 ;; esac
    printf 'yes\n'
}

gui_harness_ocr_frames() {
    local adir=$1 outdir=$2 flist=${3:-} bin=${QCI_OCR_BIN:-tesseract}
    local f n=0 ok=0 bad=0 textf=0 words=0 total=0 base stem sum bytes rc have=1
    local dec undec=0 decunk=0
    # OCR is OPTIONAL corroboration. Without a backend the manifest is still
    # written (sha256/bytes per attested frame -- which is the part that matters
    # for attestation); only the text column degrades to `skip`.
    gui_ocr_backend_probe >/dev/null 2>&1 || have=0
    mkdir -p "$outdir" 2>/dev/null || return 1
    : > "$outdir/ocr.log"
    printf 'sha256\tbytes\tframe\tdecodable\ttsv\tocr_rc\twords\n' > "$outdir/manifest.tsv"
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        n=$((n + 1))
        base=${f#"$adir"/}
        stem="frame-$(printf '%03d' "$n")-$(printf '%s' "$base" | tr -c 'A-Za-z0-9._-' '_')"
        sum=$(sha256sum "$f" 2>/dev/null | awk '{print $1}')
        [ -n "$sum" ] || sum=unknown
        bytes=$(stat -c %s "$f" 2>/dev/null || echo 0)
        words=0
        dec=$(gui_frame_is_decodable "$f")
        case "$dec" in
            no)      undec=$((undec + 1)) ;;
            unknown) decunk=$((decunk + 1)) ;;
        esac
        if [ "$dec" = no ]; then
            # Nothing can read it, OCR included. Record it and move on.
            rc=undecodable
            printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$sum" "$bytes" "$base" "$dec" "-" "$rc" 0 \
                >> "$outdir/manifest.tsv"
            printf '%s: NOT DECODABLE as an image\n' "$base" >> "$outdir/ocr.log"
            continue
        fi
        if [ "$have" = 0 ]; then
            rc=skip
            printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$sum" "$bytes" "$base" "$dec" "-" "$rc" 0 \
                >> "$outdir/manifest.tsv"
            continue
        fi
        if "$bin" "$f" "$outdir/$stem" -c tessedit_create_tsv=1 >>"$outdir/ocr.log" 2>&1 \
           && [ -f "$outdir/$stem.tsv" ]; then
            rc=0; ok=$((ok + 1))
            # A data row counts only when its text column holds a non-whitespace
            # token. tesseract emits structural rows (page/block/par/line) with an
            # empty text column; those are not "the harness read something".
            words=$(awk -F'\t' 'NR > 1 && NF >= 12 { t=$NF; gsub(/[[:space:]]/, "", t); if (t != "") c++ } END { print c+0 }' \
                "$outdir/$stem.tsv" 2>/dev/null)
            [ -n "$words" ] || words=0
            if [ "$words" -gt 0 ]; then
                textf=$((textf + 1)); total=$((total + words))
            fi
        else
            rc=1; bad=$((bad + 1))
            printf '%s: OCR invocation failed\n' "$base" >> "$outdir/ocr.log"
        fi
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$sum" "$bytes" "$base" "$dec" "$stem.tsv" "$rc" "$words" \
            >> "$outdir/manifest.tsv"
    done < <(if [ -n "$flist" ]; then cat -- "$flist"; else gui_visual_frames "$adir"; fi)
    printf 'frames=%d ocr_ok=%d ocr_fail=%d text_frames=%d words=%d undecodable=%d dec_unknown=%d\n' \
        "$n" "$ok" "$bad" "$textf" "$total" "$undec" "$decunk"
    # Success means the PASS completed and the manifest was written. OCR success
    # is reported in ocr_ok and is deliberately NOT the return value: a frame set
    # that no OCR backend could read is still fully attested evidence.
    return 0
}

# Reconcile the harness capture log against what is actually on disk in the
# harvested artifact directory. Matching is BY DIGEST, not by path: an honest
# `capture to scratch, copy into the artifact dir` still counts, and a rename
# during harvest (the short /tmp alias -> the canonical run dir) does not break
# attestation.
#
# Writes the attested-and-present frame paths, one per line, to <outfile>.
# Echoes one summary line:
#   attested=N present=P distinct=D missing_in_tree=M unharvested=U
# Returns 0 always; the caller decides.
# Args: artifact_dir capture_log outfile
gui_capture_reconcile() {
    local adir=$1 log=$2 outfile=$3
    local line seq ts vm scope bytes sum fpath chain
    local n=0 present=0 missing=0 unharv=0
    local f d base key hit
    declare -A disk_sum=() disk_taken=() present_sum=() last_row_for=()
    : > "$outfile"
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        d=$(sha256sum "$f" 2>/dev/null | awk '{print $1}')
        [ -n "$d" ] || continue
        disk_sum[$f]=$d
    done < <(gui_visual_frames "$adir")

    # A PATH HOLDS ONE FILE. A scenario that re-captures to the same path (one
    # reference-run log does it 34 times) writes a row each time, and demanding
    # a separate file per row reported the surviving frame as an omission
    # (fable, B round 4). The last row for a path supersedes the earlier ones:
    # a re-capture loop is one frame of evidence, not N.
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        IFS=$'\t' read -r seq ts vm scope bytes sum fpath chain <<<"$line"
        [ "$scope" = seal ] && continue
        # A candidate the CAPTURE TOOL judged unusable is not evidence and is
        # recorded only so the ledger stays a complete account of what the
        # harness took. It counts nowhere.
        [ "$scope" = rejected ] && continue
        [ -n "$sum" ] || continue
        [ "$scope" = in-tree ] && last_row_for[$fpath]=$seq
    done < <(gui_capture_log_rows "$log")

    while IFS= read -r line; do
        [ -n "$line" ] || continue
        IFS=$'\t' read -r seq ts vm scope bytes sum fpath chain <<<"$line"
        [ "$scope" = seal ] && continue
        [ "$scope" = rejected ] && continue
        [ -n "$sum" ] || continue
        if [ "$scope" = in-tree ] && [ "${last_row_for[$fpath]:-}" != "$seq" ]; then
            continue
        fi
        n=$((n + 1))
        # PATH FIRST, DIGEST ALWAYS. A file existing at the recorded path is not
        # enough -- an overwritten frame is a file at that path carrying other
        # bytes -- so the digest must agree either way. Harvest renames the
        # artifact directory, so the recorded ABSOLUTE path cannot be resolved
        # directly; the basename within this scenario's tree is what survives
        # that rename. A frame moved elsewhere in the tree still counts, matched
        # by digest alone.
        hit=""
        base=${fpath##*/}
        for f in "${!disk_sum[@]}"; do
            [ -n "${disk_taken[$f]:-}" ] && continue
            [ "${f##*/}" = "$base" ] || continue
            [ "${disk_sum[$f]}" = "$sum" ] || continue
            hit=$f; break
        done
        if [ -z "$hit" ]; then
            for f in "${!disk_sum[@]}"; do
                [ -n "${disk_taken[$f]:-}" ] && continue
                [ "${disk_sum[$f]}" = "$sum" ] || continue
                hit=$f; break
            done
        fi
        if [ -n "$hit" ]; then
            disk_taken[$hit]=1
            present=$((present + 1))
            if [ -z "${present_sum[$sum]:-}" ]; then
                present_sum[$sum]=1
                printf '%s\n' "$hit" >> "$outfile"
            fi
        elif [ "$scope" = in-tree ]; then
            # Captured straight into the evidence directory, then removed.
            missing=$((missing + 1))
        else
            unharv=$((unharv + 1))
        fi
    done < <(gui_capture_log_rows "$log")
    printf 'attested=%d present=%d distinct=%d missing_in_tree=%d unharvested=%d\n' \
        "$n" "$present" "${#present_sum[@]}" "$missing" "$unharv"
}

# Combined evidence decision for one scenario's harvested artifacts.
# Echoes `ok:<detail>` (accept the agent verdict) or `missing:<why>` (record
# ERROR).
# Args: artifact_dir capture_log anchor [min_captures]
# `anchor` is the gate's in-memory "<vm>\t<total_rows>\t<chain_head>" from
# gui_capture_log_seal. An EMPTY anchor is fail-closed: an unsealed ledger is
# one nobody froze, so it is not graded.
gui_visual_evidence_status() {
    local adir=$1 caplog=${2:-} anchor=${3:-} mincaps=${4:-1}
    local outdir ocr summary frames ok_n bad_n textf words stale
    local capsum caprows recon attested present distinct missing unharv flist sfacts
    local undec decunk
    local a_vm="" a_rows="" a_head=""
    IFS=$'\t' read -r a_vm a_rows a_head <<<"$anchor"
    if [ ! -d "$adir" ]; then
        printf 'missing:the scenario left no artifact directory, so no frame could be read\n'
        return 1
    fi
    outdir="$adir/$GUI_VISUAL_EVIDENCE_DIR"
    # Fail closed against an agent that pre-creates the harness-owned directory
    # to plant results: move anything already there aside, never read it.
    if [ -e "$outdir" ] || [ -L "$outdir" ]; then
        stale="$adir/$GUI_VISUAL_EVIDENCE_DIR.agent-supplied"
        rm -rf -- "$stale" 2>/dev/null || true
        mv -- "$outdir" "$stale" 2>/dev/null || rm -rf -- "$outdir" 2>/dev/null || true
    fi
    # OCR is corroboration, not a precondition. It reads TEXT and nothing else:
    # it cannot establish colour, layout/geometry, focus, z-order, animation or
    # the ABSENCE of a control, which is most of what these scenarios assert. So
    # its absence degrades the text column and nothing more -- making it fatal
    # would void verdicts over a tool that could not have adjudicated them. What
    # IS load-bearing is below: the sealed, VM-bound ledger and the presence of
    # the attested frames.
    ocr=$(gui_ocr_backend_probe) || ocr=""
    : "${ocr:=none}"

    # ATTESTATION FIRST. What makes a frame evidence is that this scenario's
    # sealed, VM-bound ledger records it -- not that an image file exists, and
    # not that OCR found words in it. (A ledger row is not proof that a capture
    # happened; see the contract block for the exact claim.) Everything below
    # grades only the attested-and-present set.
    # Only meaningful once the ledger itself is there; an absent/unprovisioned
    # ledger has its own (more useful) message from the verifier below.
    if [ -n "$caplog" ] && [ ! -L "$caplog" ] && [ -f "$caplog" ] \
       && { [ -z "$anchor" ] || [ -z "$a_head" ]; }; then
        printf 'missing:the gate could not SEAL this scenario'"'"'s capture ledger after the agent exited, so it cannot vouch that the ledger it is reading is the one it provisioned\n'
        return 1
    fi
    if ! capsum=$(gui_capture_log_verify "$caplog" "$a_vm" "$a_rows" "$a_head"); then
        printf 'missing:%s, so the gate cannot tell which images (if any) it actually captured from the VM\n' \
            "${capsum#bad:}"
        return 1
    fi
    caprows=$(printf '%s' "$capsum" | sed -nE 's/^rows=([0-9]+) .*/\1/p')
    : "${caprows:=0}"
    if [ "$caprows" -eq 0 ]; then
        printf 'missing:the harness took NO capture from this scenario VM (neither vm-gui screenshot/click-preview nor the qdwin in-guest capture helper ran), so every image under the artifact directory is agent-authored and none of it is evidence\n'
        return 1
    fi
    mkdir -p "$outdir" 2>/dev/null || {
        printf 'missing:the gate could not create its own evidence directory under %s, so it could not read the frames\n' "$adir"
        return 1
    }
    cp -- "$caplog" "$outdir/captures.tsv" 2>/dev/null || true
    flist="$outdir/attested-frames.txt"
    recon=$(gui_capture_reconcile "$adir" "$caplog" "$flist")
    attested=0; present=0; distinct=0; missing=0; unharv=0
    read -r attested present distinct missing unharv < <(printf '%s\n' "$recon" | sed -nE \
        's/^attested=([0-9]+) present=([0-9]+) distinct=([0-9]+) missing_in_tree=([0-9]+) unharvested=([0-9]+)$/\1 \2 \3 \4 \5/p') || true
    : "${attested:=0}" "${present:=0}" "${distinct:=0}" "${missing:=0}" "${unharv:=0}"
    # DECLARED CAPTURE COUNT. The one mechanism that can distinguish "the agent
    # never captured the failing moment" from "the agent captured it and
    # dropped the row before exiting" — because the SCENARIO, not the agent,
    # says how many frames must exist. Floor of 1 when undeclared.
    #
    # It is checked against the RECONCILED count, not the verifier's raw row
    # total: the raw total includes `rejected` rows and superseded re-captures
    # to the same path, neither of which is a frame of evidence. Under the
    # earlier two-rows-per-frame design a raw-row floor also counted one
    # screenshot as two, so a scenario declaring 2 passed on one frame (sol,
    # B round 2).
    if [ "$attested" -lt "$mincaps" ]; then
        printf 'missing:this scenario declares <!-- qci:visual-captures: %s --> but the harness took only %d capture(s) from its VM, so the frames the scenario requires were never taken (or were dropped before the agent exited)\n' \
            "$mincaps" "$attested"
        return 1
    fi
    if [ "$missing" -gt 0 ]; then
        printf 'missing:%d of %d frame(s) the harness captured INTO this scenario'"'"'s artifact directory are no longer there (%s/captures.tsv lists them). A capture that was taken and then removed is frame omission, which is cheaper than forgery and hides exactly the failing frame\n' \
            "$missing" "$attested" "$GUI_VISUAL_EVIDENCE_DIR"
        return 1
    fi
    if [ "$present" -eq 0 ]; then
        printf 'missing:the harness captured %d frame(s) but none of them was harvested into the artifact directory (%d written outside it; see %s/captures.tsv), so there is no attested pixel evidence to read\n' \
            "$attested" "$unharv" "$GUI_VISUAL_EVIDENCE_DIR"
        return 1
    fi

    summary=$(gui_harness_ocr_frames "$adir" "$outdir" "$flist") || true
    # Parse the whole summary in ONE anchored match. A per-key `.*frames=` grep
    # is wrong here: greedy `.*` lets `frames=` match inside `text_frames=`.
    frames=0; ok_n=0; bad_n=0; textf=0; words=0; undec=0; decunk=0
    read -r frames ok_n bad_n textf words undec decunk < <(printf '%s\n' "$summary" | sed -nE \
        's/^frames=([0-9]+) ocr_ok=([0-9]+) ocr_fail=([0-9]+) text_frames=([0-9]+) words=([0-9]+) undecodable=([0-9]+) dec_unknown=([0-9]+)$/\1 \2 \3 \4 \5 \6 \7/p') || true
    : "${frames:=0}" "${ok_n:=0}" "${bad_n:=0}" "${textf:=0}" "${words:=0}" "${undec:=0}" "${decunk:=0}"
    if [ -z "$summary" ]; then
        # The OCR pass could not even start (its output directory is not
        # writable). Fail closed, and say so rather than blaming the scenario.
        printf 'missing:the gate could not create its own evidence directory under %s, so it could not read the frames\n' "$adir"
        return 1
    fi
    # The bar is at least ONE frame the gate POSITIVELY decoded. `unknown` is
    # not a pass: it means no decoder was installed, so the gate never looked at
    # a single pixel and cannot tell a screenshot from a text file. Grading on
    # `unknown` was the escape astra found in B round 1 -- a sealed, correctly
    # VM-bound ledger holding a 12-byte file reading `not an image` retained
    # both PASS and FAIL, because the old test here was `undec -eq frames` and
    # an undecided frame is not an undecodable one. Whether the decoder is
    # ABSENT or the frames are BROKEN, the consequence for the verdict is
    # identical and fail-closed: no readable pixel evidence in the tree, exactly
    # like "no frames at all". A PARTIAL count is REPORTED below and changes no
    # verdict -- that is a diagnosis signal, and turning it into a failure would
    # punish a scenario for one flaky capture.
    if [ "$frames" -gt 0 ] && [ "$((frames - undec - decunk))" -le 0 ]; then
        if [ "$decunk" -eq "$frames" ]; then
            printf 'missing:NO IMAGE DECODER is installed on this host (ImageMagick `magick` not found), so the gate could not decode any of the %d attested frame(s) and never read a pixel (%s/manifest.tsv, decodable=unknown). This is a missing harness capability, not a verdict -- install ImageMagick\n' \
                "$frames" "$GUI_VISUAL_EVIDENCE_DIR"
        elif [ "$undec" -eq "$frames" ]; then
            printf 'missing:all %d attested frame(s) in this scenario'"'"'s artifact directory are UNDECODABLE as images (%s/manifest.tsv, decodable=no), so there is no readable pixel evidence -- this is a capture/harvest failure, not a verdict\n' \
                "$frames" "$GUI_VISUAL_EVIDENCE_DIR"
        else
            printf 'missing:NOT ONE of the %d attested frame(s) could be decoded as an image -- %d are broken and %d were never checked because no decoder is installed (%s/manifest.tsv). There is no readable pixel evidence, so this is a capture/harness failure, not a verdict\n' \
                "$frames" "$undec" "$decunk" "$GUI_VISUAL_EVIDENCE_DIR"
        fi
        return 1
    fi
    if [ "$frames" -eq 0 ]; then
        printf 'missing:this scenario captured NO frame into its artifact directory, so there was nothing for the gate to read\n'
        return 1
    fi
    # Every OCR invocation failing is recorded, not fatal -- see the note above
    # the probe. The frames remain attested; only the text column is empty.
    # Structural facts the HARNESS can establish about the attested set without
    # any vision model: how many captures it took, how many of them are
    # byte-distinct, and how many it took outside the evidence tree. `distinct`
    # is the "nothing happened" signal for a scenario that captures a before and
    # an after -- but it is REPORTED, never verdict-affecting, because identical
    # frames are the correct result for the stability scenarios that exist here
    # (e.g. qdwin-noctalia/05 "bar stays after idle"). Turning it into a FAIL
    # would manufacture wrong verdicts; surfacing it lets report.py and a human
    # see a degenerate capture set at a glance.
    sfacts=$(printf 'harness-captured %d frame(s), %d distinct' "$present" "$distinct")
    [ "$undec" -gt 0 ] && sfacts="$sfacts, $undec UNDECODABLE (see manifest.tsv decodable=no)"
    [ "$decunk" -gt 0 ] && sfacts="$sfacts, $decunk not checked for decodability (no ImageMagick)"
    [ "$unharv" -gt 0 ] && sfacts="$sfacts, $unharv captured outside the artifact dir and not graded"
    if [ "$textf" -gt 0 ]; then
        printf 'ok:the gate read the pixels of this scenario'"'"'s attested frame set itself (sealed, VM-bound ledger) — %s (%s/captures.tsv); %s read text in %d frame(s) (%d words; %s/manifest.tsv). OCR evidences TEXT only — colour, layout, focus, z-order and absence claims are NOT adjudicated, only attested as observed\n' \
            "$sfacts" "$GUI_VISUAL_EVIDENCE_DIR" "$ocr" "$textf" "$words" "$GUI_VISUAL_EVIDENCE_DIR"
        return 0
    fi
    # "OCR ran and found nothing" is NOT "no OCR happened", and after round 5 it
    # is not a bypass either: these frames are attested harness captures of this
    # scenario's VM, so a textless one is a real observation of a screen with no
    # legible text. The blank-image bypass died at the attestation check above,
    # not here — which is why this can stay an observation without re-opening it.
    if [ "$ocr" = none ]; then
        printf 'ok:this scenario'"'"'s frame set is attested by its sealed, VM-bound ledger — %s (%s/captures.tsv); no OCR backend on this host, so the per-frame text column is `skip` (%s/manifest.tsv still records each frame'"'"'s sha256 and size). Text corroboration is unavailable; the verdict does not rest on it\n' \
            "$sfacts" "$GUI_VISUAL_EVIDENCE_DIR" "$GUI_VISUAL_EVIDENCE_DIR"
        return 0
    fi
    printf 'ok:the gate read the pixels of this scenario'"'"'s attested frame set itself (sealed, VM-bound ledger) — %s (%s/captures.tsv); %s read NO text in any of them (%d OCR failure(s); %s/manifest.tsv). The frames are attested for this VM, but this verdict rests on something OCR cannot read\n' \
        "$sfacts" "$GUI_VISUAL_EVIDENCE_DIR" "$ocr" "$bad_n" "$GUI_VISUAL_EVIDENCE_DIR"
    return 0
}

# THE ENFORCEMENT POINT. Sits between harvest/agent_artifact_status and the
# verdict mapping in gui_run_scenario: a visual scenario's PASS or FAIL is
# accepted only when the HARNESS was able to read this scenario's frames.
# Everything else is passed through untouched — SKIP and UNKNOWN make no pixel
# claim, and a `qci:visual: none` scenario is not subject to the contract.
#
# There is deliberately NO bypass. An env var that re-accepted an unevidenced
# PASS would re-open exactly the hole this closes, so none is offered. What the
# gate requires is ATTESTED FRAMES -- a sealed, VM-bound ledger whose captures
# are present in the artifact tree. OCR is corroboration recorded alongside
# them and is never the thing that makes a scenario gradable.
#
# Echoes "<status>\t<note-suffix>".
# Args: status scenario_file artifact_dir capture_log anchor
# `anchor` is the gate's in-memory seal ("<vm>\t<rows>\t<head>"); without it
# nothing is graded.
gui_apply_visual_evidence_contract() {
    local status=$1 scenario=$2 adir=$3 caplog=${4:-} anchor=${5:-} ev mode mincaps
    case "$status" in
        PASS|FAIL) ;;
        *) printf '%s\t\n' "$status"; return 0 ;;
    esac
    mode=$(gui_scenario_visual_mode "$scenario")
    case "$mode" in
        none) printf '%s\t\n' "$status"; return 0 ;;
        required) ;;
        *)
            printf 'ERROR\tvisual-evidence contract: %s — every GUI scenario must declare <!-- qci:visual: required --> or <!-- qci:visual: none -->; the agent verdict %s is not graded until it does\n' \
                "${mode#invalid:}" "$status"
            return 0
            ;;
    esac
    mincaps=$(gui_scenario_min_captures "$scenario")
    if ev=$(gui_visual_evidence_status "$adir" "$caplog" "$anchor" "$mincaps"); then
        printf '%s\tvisual-evidence %s\n' "$status" "${ev#ok:}"
        return 0
    fi
    printf 'ERROR\tvisual-evidence contract: %s; the agent verdict %s concerns pixels the HARNESS could not read, so it is not graded\n' \
        "${ev#missing:}" "$status"
}

# Extract WHY a scenario reported SKIP, from the artifacts the agent left.
#
# gui_agent_verdict is pure (status + rc only) and returns the fixed note "agent
# scenario skipped", which gui_run_scenario wrote verbatim into results.tsv. So
# every skipped GUI scenario looked identical in the results, and report.py's
# dependency-missing detector could not tell a GOLDEN-IMAGE GAP ("foot is not
# installed in the guest") from a legitimately not-applicable scenario. The
# latest full run's qdlocker/tests/gui/01-lock-cycle.md is exactly the first
# kind, and it cost 747 seconds to reach that conclusion invisibly.
#
# Sources, in order of preference:
#   1. text after the verdict token in status.txt (agents that write "SKIP <why>")
#   2. the report.md "Result: ..." line (the observed house style)
#   3. the first non-empty prose line of report.md that is not its heading
# Backticks and markdown links are flattened, ALL control characters and Unicode
# line separators are stripped (the value lands in a TSV column that report.py
# reads with splitlines(), which breaks on far more than \n), and the result is
# length-capped. Input is also size-bounded before parsing — the cap on the
# OUTPUT is not a bound on how much is read. Echoes nothing when no reason can be
# found — the caller then keeps its generic note.
# Args: artifact_dir
gui_skip_reason() {
    local adir=$1 reason=""
    # head -c bounds the READ. A status.txt of arbitrarily many blank lines, or a
    # report.md whose first "record" is gigabytes long, would otherwise be parsed
    # in full before the output cap ever applied.
    local _cap=65536
    if [ -f "$adir/status.txt" ] && [ ! -L "$adir/status.txt" ]; then
        # "SKIP foo not installed" -> "foo not installed"; bare "SKIP" -> "".
        reason=$(head -c "$_cap" < "$adir/status.txt" | tr -d '\r' \
            | awk 'NF { $1=""; sub(/^[[:space:]]+/, ""); print; exit }')
    fi
    if [ -z "$reason" ] && [ -f "$adir/report.md" ] && [ ! -L "$adir/report.md" ]; then
        reason=$(head -c "$_cap" < "$adir/report.md" | awk '
            NR > 40 { exit }
            /^[[:space:]]*Result:/ { sub(/^[[:space:]]*Result:[[:space:]]*/, ""); print; exit }
        ')
    fi
    if [ -z "$reason" ] && [ -f "$adir/report.md" ] && [ ! -L "$adir/report.md" ]; then
        reason=$(head -c "$_cap" < "$adir/report.md" | awk '
            NR > 40 { exit }
            /^[[:space:]]*#/ { next }
            NF { print; exit }
        ')
    fi
    [ -n "$reason" ] || return 0
    # Flatten markdown noise, then strip EVERY control character and the Unicode
    # line separators. Removing only tab/LF is not enough: report.py reads the
    # TSV with Python splitlines(), which also breaks on \x0b \x0c \x1c \x1d
    # \x1e \x85 U+2028 U+2029. An agent-written reason containing one of those
    # would split a single result into two malformed report rows (and an ESC
    # would inject a terminal escape sequence into the report). This text comes
    # from an artifact the agent wrote, so it is untrusted input to the TSV.
    # Shared with the bats companion-row path via tsv_note_sanitize (ci/lib/core.sh)
    # so the two note paths cannot drift apart (they did: bats stripped only \t).
    reason=$(printf '%s' "$reason" | tsv_note_sanitize)
    printf '%s' "${reason:0:300}"
}

# Copy the guest-side waiter library (ci/lib/guest/gui-waiters.sh) into a
# disposable VM at /tmp/qci-gui-waiters.sh so markdown scenarios (and the agent)
# can `source /tmp/qci-gui-waiters.sh`. Delivered base64 over vm-exec — NOT a
# shared HTTP port (the single-tenant lane rule) — from the VERSIONED repo copy,
# so a waiter-library change is exercised against current source without rebaking
# any image. bash -n in the guest verifies the delivered file parses. Best-effort
# from the caller's view: returns nonzero (and the caller logs) on failure; a
# scenario that genuinely needs the waiters and lacks them fails its own
# assertion loudly rather than passing silently.
install_gui_waiters() {
    local vm=$1 src="$QDISTRO_REPO/ci/lib/guest/gui-waiters.sh" b64
    [ -f "$src" ] || { log "install_gui_waiters: source missing: $src"; return 1; }
    b64=$(base64 -w0 < "$src" 2>/dev/null) || b64=$(base64 < "$src" | tr -d '\n')
    "$VM_TOOLS/vm-exec" "$vm" \
        "printf '%s' '$b64' | base64 -d > /tmp/qci-gui-waiters.sh && bash -n /tmp/qci-gui-waiters.sh" \
        >/dev/null 2>&1
}

# Best-effort: keep the qdlocker idle lock from firing mid-scenario on long agent
# GUI runs. A multi-minute agent session otherwise trips the production 5-minute
# idle lock; the lock screen then appears mid-run and the agent burns its whole
# budget fighting it instead of testing the scenario (observed in apps/10 and
# permissions-gui/21, where the "app" screenshots were actually the lock screen).
#
# Installs the SAME 24h-idle dropin that qdlocker_prepare_gui_lane uses
# (90-ci-gui.conf), but for EVERY agent GUI VM — not just the qdlocker scenarios
# that source qdlocker-helpers.sh. The 90- prefix sorts BEFORE the `idle.conf`
# that the dedicated idle scenarios (qdlocker/03-idle-lock-trigger,
# 04-lid-close-lock) write, so those tests' shorter override still wins and the
# idle path stays under test. Best-effort: no-op when the admin session user is
# absent, and any reload/restart failure is swallowed so it can't abort a run.
suppress_idle_lock() {
    local vm=$1 b64 script
    script='set -e
# Only meaningful on a VM that has the admin session user; bail harmlessly
# otherwise. Create the drop-in dir unconditionally (install -d makes parents),
# so a VM whose per-user tree is not pre-populated still gets idle suppression.
id admin >/dev/null 2>&1 || exit 0
d=/home/admin/.config/systemd/user/qdlocker.service.d
install -d -m 0755 -o admin -g users "$d"
cat >"$d/90-ci-gui.conf" <<EOF
[Service]
Environment=QDLOCKER_IDLE_MS=86400000
EOF
chown admin:users "$d/90-ci-gui.conf"
runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user daemon-reload 2>/dev/null || exit 0
runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart qdlocker.service 2>/dev/null || true'
    b64=$(printf '%s' "$script" | base64 -w0 2>/dev/null) || b64=$(printf '%s' "$script" | base64 | tr -d '\n')
    "$VM_TOOLS/vm-exec" "$vm" "printf '%s' '$b64' | base64 -d | bash" >/dev/null 2>&1 || true
}

# Host-side waiter: retry a guest command over vm-exec until it exits 0 or the
# deadline passes — the host equivalent of the guest await_* helpers, for
# host-driven readiness gates. Returns 0 on success, 1 on timeout; on timeout
# logs the (ceiled) last guest output. VM_TOOLS is overridable so this is
# host-testable with a fake vm-exec.
# Args: vm timeout_s interval_s <guest-cmd...>
#
# WHY THE OUTPUT GOES THROUGH A FILE AND NOT THROUGH `$( ... 2>&1 )`.
# `last=$(vm-exec ... 2>&1)` hands vm-exec's fd 1 AND fd 2 to this
# substitution's pipe. vm-exec bounds its children's fd 1 internally, but fd 2
# is inherited straight through to every virsh/jq descendant it starts. A
# descendant that outlives vm-exec and keeps that descriptor holds the
# substitution open, and bash waits for the pipe to close, not for vm-exec to
# exit -- so the call can hang after the command it ran is long dead, with no
# timeout able to help. A read from a REGULAR file has no such dependency: it
# reaches EOF at the current end of file however many writers still hold it.
# (An actively growing file keeps producing more on a later read; that is why
# the read below is also ceiled, and why the file is per-call.)
#
# WHY A FRESH FILE PER ATTEMPT, UNLINKED BEFORE THE COMMAND STARTS.
# The previous shape truncated ONE file per attempt (`: > "$cf"`). That is the
# same isolation error this file's own vm-exec fixed one level inward: a
# descendant of attempt N that still holds fd 2 on that inode appends into
# attempt N+1's capture, so the text logged for one attempt can be another
# attempt's output. Each attempt now gets its own mktemp name, opens both ends,
# and unlinks the name BEFORE the command runs -- so no later attempt can be
# handed an inode an earlier attempt's survivor still holds, and no file is
# left named while a command is running. This mirrors bounded_run() in
# scripts/vm/vm-exec.
#
# WHAT THIS DOES NOT DO: it does not reap or bound a surviving descendant. Such
# a writer keeps an unlinked inode and can consume disk; the size bound on it is
# whatever RLIMIT_FSIZE vm-exec installed on its own children (vm-exec refuses
# to start without one unless QDISTRO_VM_ALLOW_UNBOUNDED_CAPTURE=1). This
# helper adds no limit of its own beyond the ceiled read.
#
# THE DEADLINE IS ENFORCED, NOT MERELY CHECKED AFTER THE FACT.
# The old loop invoked vm-exec and only compared elapsed time once it returned,
# so an invocation that never returned never reached the check and a 2s success
# was accepted against a 1s budget. Each invocation is now run under
# `timeout -k <grace> <remaining>s`, where <remaining> is what is left of the
# caller's budget, and the budget is re-checked at the top of every iteration.
# Consequently: no single invocation can outlast the budget, and a success that
# arrives after the budget is spent cannot be returned as a success -- `timeout`
# has killed it and the status is 124/137.
#
# THE BOUND, STATED WITH ITS CONDITIONS. Once the budget is spent this function
# returns within <grace> seconds plus one ceiled read, ASSUMING `timeout` is
# GNU coreutils' (KILL after grace is not deniable by the monitored process)
# and that the local read/unlink make ordinary progress. The wall time before
# that is at most timeout_s + interval_s + grace + the reads + THE CAPTURE
# SETUP of the final attempt. That last term used to be omitted; it is not
# negligible and it is not hypothetical -- a two-second mktemp against a
# one-second budget was enough to return a success past the deadline before
# the budget was recomputed after setup (sol, qci-A-260917-sol-review.md
# section 4). The recomputation bounds the damage; it does not make setup free.
# One visible side effect of the `$( )` replay, shared by every site that
# uses it: on a NUL-bearing capture bash writes `warning: command
# substitution: ignored null byte in input` to stderr, once per call.
# Harmless, and not a failure.
GUI_AWAIT_CAP_BYTES=${QCI_GUI_AWAIT_CAP_BYTES:-65536}
# A cap that is not a positive decimal integer is not a byte ceiling: GNU
# `head -c -1` means "all but the LAST byte" and `head -c 00` reads nothing,
# both of which look like a working bound (sol A2 section 2).
case "$GUI_AWAIT_CAP_BYTES" in
    ''|*[!0-9]*|0|0*)
        echo "gui gate: GUI_AWAIT_CAP_BYTES must be a positive integer number of bytes, got '$GUI_AWAIT_CAP_BYTES'" >&2
        return 2 2>/dev/null || exit 2 ;;
esac
GUI_AWAIT_KILL_GRACE=${QCI_GUI_AWAIT_KILL_GRACE:-5}
await_vmexec_success() {
    local vm=$1 timeout=$2 interval=$3; shift 3
    local start=$SECONDS last="" elapsed left rc cf wfd rfd
    while :; do
        elapsed=$((SECONDS - start))
        left=$((timeout - elapsed))
        if [ "$left" -le 0 ]; then
            log "await_vmexec_success: TIMEOUT ${elapsed}s on '$*' (last: ${last:0:200})"
            return 1
        fi
        cf=$(mktemp "${TMPDIR:-/tmp}/qci-await.XXXXXXXX") || {
            log "await_vmexec_success: cannot create a capture file; refusing to run '$*'"
            return 1
        }
        # Open both ends, then unlink: from here on the inode has no pathname,
        # so neither a survivor of this attempt nor any later attempt can reach
        # it by name. EVERY step is checked. An unchecked `rm` here used to let
        # the function run the command anyway and return its status, leaving a
        # NAMED capture behind and silently voiding the invariant this comment
        # claims (sol, qci-A-260917-sol-review.md §3, reproduced by injecting a
        # failing rm). A setup failure is infrastructure, not a guest result.
        if ! exec {wfd}>"$cf"; then
            log "await_vmexec_success: cannot open capture for write; refusing to run '$*'"
            rm -f "$cf"
            return 1
        fi
        if ! exec {rfd}<"$cf"; then
            log "await_vmexec_success: cannot open capture for read; refusing to run '$*'"
            exec {wfd}>&-
            rm -f "$cf"
            return 1
        fi
        if ! rm -f "$cf" || [ -e "$cf" ]; then
            log "await_vmexec_success: capture file still NAMED after unlink; refusing to run '$*'"
            exec {wfd}>&- {rfd}<&-
            return 1
        fi
        rc=0
        # Recompute the budget HERE. It used to be the `left` computed before
        # mktemp and the three opens, so everything that setup cost was spent
        # out of the caller's deadline without being counted against it.
        elapsed=$((SECONDS - start))
        left=$((timeout - elapsed))
        if [ "$left" -le 0 ]; then
            exec {wfd}>&- {rfd}<&-
            log "await_vmexec_success: TIMEOUT ${elapsed}s on '$*' (capture setup consumed the budget)"
            return 1
        fi
        # The command and every descendant get the capture FILE on fd 1 and
        # fd 2. The bookkeeping descriptors are closed for the child so nothing
        # downstream inherits a second handle or the read end.
        timeout -k "${GUI_AWAIT_KILL_GRACE}s" "${left}s" \
            "$VM_TOOLS/vm-exec" "$vm" "$*" >&"$wfd" 2>&"$wfd" {wfd}>&- {rfd}<&- || rc=$?
        exec {wfd}>&-
        # Bounded by BYTES, not by representable characters. `read -N` counts
        # characters and silently DELETES NUL bytes on the way, so a capture
        # full of NULs made it read far past the nominal ceiling (4 MiB of NULs
        # cost 4,194,305 bytes and ~0.88s in sol's measurement). `head -c` is a
        # fork, which is why the ceiling is only consulted once per attempt.
        last=""
        # A FAILED replay is not empty output. This waiter's VERDICT is the
        # producer's exit status, not the capture -- the capture is diagnostic
        # only -- so a lost capture must NOT flip a genuine success into a
        # failure; that would be a new false-red. What it must not do is pass
        # silently: `|| :` left a failed `head` looking exactly like a command
        # that printed nothing (astra, A-astra finding 3).
        #
        # NOTE the scope, because I previously claimed the opposite: unlike the
        # capture helpers, this site does NOT return 125 on a replay failure,
        # and it is a deliberate exception rather than an oversight. It says so
        # out loud and puts the marker in the timeout diagnostic below.
        if ! last=$(head -c "$GUI_AWAIT_CAP_BYTES" <&"$rfd"); then
            last="<capture replay FAILED: the command's output is UNAVAILABLE, not empty>"
            log "await_vmexec_success: could not replay the capture for '$*'; the readiness verdict below rests on the exit status alone, with no output to show for it"
        fi
        exec {rfd}<&-
        # A success is only a success WITHIN the deadline. Returning 0 here
        # without re-checking meant a command that finished after the budget
        # expired was reported ready: with a 1s budget and a 2s setup delay
        # this returned 0 at 2.01s (sol §4).
        elapsed=$((SECONDS - start))
        if [ "$rc" -eq 0 ]; then
            if [ "$elapsed" -ge "$timeout" ]; then
                log "await_vmexec_success: succeeded at ${elapsed}s but the ${timeout}s deadline had passed on '$*' — reporting NOT ready"
                return 1
            fi
            return 0
        fi
        if [ "$elapsed" -ge "$timeout" ]; then
            log "await_vmexec_success: TIMEOUT ${elapsed}s on '$*' (last: ${last:0:200})"
            return 1
        fi
        sleep "$interval"
    done
}

# Pure status/rc -> verdict mapper for one agent scenario attempt. FAIL CLOSED:
# a pass is recorded ONLY for an explicit PASS with rc=0. A SKIP is honoured ONLY
# with rc=0 — a SKIP artifact left behind by a process that timed out, was killed,
# or died in its tooling is NOT an intentional skip, and accepting it masked a
# real 721s rc=124 timeout as skip/exit-0 in full-20260628T111224Z-3231467
# (scenario-attempts.tsv row 19 vs results.tsv row 149). SKIP with a nonzero rc is
# therefore a hard failure, classified like any other. Everything else — FAIL/ERROR, UNKNOWN (agent exited without a parseable
# status.txt/report verdict), PASS-with-nonzero-rc (claimed PASS but the runner
# returned nonzero), or any malformed combination — is a hard GUI failure, never
# a silent green. The agent prompt's contract is "return 0 only when every
# required assertion passes", so a nonzero rc on a PASS is a contradiction and
# stays red. Echoes a single TAB-separated line "<verdict>\t<note>" where
# <verdict> is pass|skip|fail. Pure (reads only its args) so it is host-testable
# without the GUI VM stack — see tests/integration/qci/gui-agent-verdict.bats.
# Args: status rc
gui_agent_verdict() {
    local status=$1 rc=$2
    case "$status:$rc" in
        PASS:0)
            printf 'pass\tagent scenario passed' ;;
        SKIP:0)
            printf 'skip\tagent scenario skipped' ;;
        SKIP:*)
            printf 'fail\tagent status=SKIP rc=%s (skip artifact with nonzero rc — fail closed)' "$rc" ;;
        FAIL:*|ERROR:*)
            printf 'fail\tagent status=%s rc=%s' "$status" "$rc" ;;
        *)
            printf 'fail\tagent command rc=%s status=%s (no usable verdict — fail closed)' "$rc" "$status" ;;
    esac
}

# Pure failure classifier (Phase 6). Maps a FAILING agent attempt to a MECHANICAL
# signature — the ONLY basis on which an automatic retry may ever be considered.
# It NEVER classifies a product/test failure as retriable:
#   product-fail      status=FAIL   (agent ran the asserts; one failed)       NEVER retry
#   product-error     status=ERROR  (agent couldn't set up preconditions)     NEVER retry
#   external-network  status=ERROR + an unambiguous guest-side external FETCH
#                     failure marker (curl/zypper/registry download reset/timeout/
#                     DNS) during setup: an upstream CDN/mirror/registry outage,
#                     pure INFRA — never a product regression. NOT auto-retriable
#                     (re-running does not fix an upstream outage), but joins the
#                     correlated-burst allowlist and is bucketed non-actionable in
#                     the report so a CDN blip is not read as a product failure.
#   no-verdict        UNKNOWN:0      (agent exited clean with no verdict)      NEVER retry
#   agent-tooling     status=FAIL + an unambiguous SHELL COMMAND-CONSTRUCTION
#                     error in the agent's OWN driver commands (malformed bash/sh
#                     -c wrapper, unterminated quote): the FAIL is NOT a trustworthy
#                     product result because the assertion target never ran.  retriable
#   transport-timeout UNKNOWN + rc=124 + an unambiguous qemu GUEST-AGENT
#                     CONNECTIVITY-loss marker in the log (the host could not talk
#                     to the guest agent AT ALL) — pure INFRA                retriable
#   agent-api-unreachable
#                     UNKNOWN + an unambiguous LLM-PROVIDER connectivity-loss
#                     marker in the log (the agent CLI could not open a socket to
#                     the API at all — "API Error: Unable to connect to API
#                     (FailedToOpenSocket|ConnectionRefused)"). Pure INFRA, and
#                     independent of rc: the observed outage produced BOTH rc=0
#                     and rc=1, so the provider marker — not the rc — is the
#                     discriminator. Checked before no-verdict so a clean-exit
#                     outage is not miscounted.                             retriable
#   agent-api-after-verdict
#                     PASS + nonzero rc + the same exact provider marker. The
#                     agent wrote a positive artifact but its process did not
#                     complete successfully, so the contradiction stays red;
#                     a fresh-VM retry may resolve the provider-only epilogue.
#                                                                            retriable
#   agent-timeout     UNKNOWN or SKIP + rc=124, no connectivity marker: the agent ran out
#                     of budget. DELIBERATELY NOT auto-retriable — a slow agent can
#                     equally mean the PRODUCT hung, and retrying could flake-pass a
#                     real hang (codex). Surfaced for human triage / a Phase-5
#                     scenario split instead.                              report-only
#   unknown           anything else (incl. PASS:nonzero without the exact provider
#                     marker, or a SKIP artifact with a nonzero rc that is not 124 —
#                     inconsistent, NOT an unambiguous infra retry)
#                                                                            NEVER retry
# Keying on the PARSED status=UNKNOWN (not merely "no status.txt") is deliberate:
# a partial report.md verdict + rc=124 is an inconsistent agent result, not an
# infra timeout. Pure (args only) => host-testable (gui-retry-classify.bats).
# A nonzero-rc SKIP reaches here because gui_agent_verdict now fails it closed.
# Args: status agent_rc transport_marker(0/1) agent_tooling_marker(0/1, optional)
#       agent_api_marker(0/1, optional)
gui_classify_failure() {
    local status=$1 rc=$2 transport=$3 tooling=${4:-0} api=${5:-0} extnet=${6:-0}
    case "$status" in
        FAIL)
            # A FAIL whose evidence shows the agent's OWN command was malformed is
            # not a trustworthy product result (the assertion target never ran).
            # Only status=FAIL is flipped — ERROR/PASS/SKIP/UNKNOWN are untouched.
            # A provider-unreachable marker does NOT flip a FAIL: the agent ran the
            # asserts and one genuinely failed; the marker's scope is UNKNOWN only.
            if [ "$tooling" = 1 ]; then printf 'agent-tooling'; else printf 'product-fail'; fi
            return ;;
        ERROR)
            # An ERROR caused by an upstream external FETCH failure (curl/zypper/
            # registry) during setup is infra, not a product/setup bug. Checked
            # before the plain product-error so a CDN outage is not miscounted.
            if [ "$extnet" = 1 ]; then printf 'external-network'; return; fi
            printf 'product-error'; return ;;
        PASS)
            # Keep gui_agent_verdict fail-closed: PASS with nonzero rc is never
            # accepted directly. When the exact provider marker explains the
            # nonzero epilogue, however, it is safe to retry on a fresh VM. This
            # covers Codex writing status.txt/report.md and then receiving the
            # selected-model-capacity error while finalizing its response.
            if [ "$rc" != 0 ] && [ "$api" = 1 ]; then
                printf 'agent-api-after-verdict'; return
            fi ;;
        SKIP)
            # Only reachable with a nonzero rc (gui_agent_verdict accepts SKIP:0
            # as a skip). The SKIP file is not evidence the process finished, so
            # the rc decides: 124 is a budget timeout, exactly as for UNKNOWN, and
            # is report-only for the same product-hang-masking reason. Anything
            # else stays `unknown` and is never auto-retried.
            if [ "$rc" = 124 ]; then
                if [ "$transport" = 1 ]; then printf 'transport-timeout'; else printf 'agent-timeout'; fi
                return
            fi ;;
        UNKNOWN)
            # LLM-provider connectivity loss is pure infra and rc-independent (the
            # observed outage produced both rc=0 and rc=1), so it is checked FIRST
            # — before the rc=0 no-verdict branch — or a clean-exit outage would be
            # miscounted as no-verdict and a rc=1 outage as generic `unknown`.
            if [ "$api" = 1 ]; then printf 'agent-api-unreachable'; return; fi
            if [ "$rc" = 0 ]; then printf 'no-verdict'; return; fi
            if [ "$rc" = 124 ]; then
                if [ "$transport" = 1 ]; then printf 'transport-timeout'; else printf 'agent-timeout'; fi
                return
            fi ;;
    esac
    printf 'unknown'
}

# Pure: is a classifier eligible for an AUTOMATIC retry? ONLY the unambiguous
# infra signatures. agent-timeout is intentionally excluded (product-hang masking
# risk); product-fail/error/no-verdict/unknown are never retriable by definition.
# agent-api-unreachable and agent-api-after-verdict are pure LLM-provider
# failures (no product/guest failure signal) and are safe to re-run on a fresh
# attempt. The latter still requires a fresh PASS; its first PASS is never
# accepted directly because the process rc contradicted it.
# Args: classifier
gui_classifier_retriable() {
    case "$1" in
        transport-timeout|agent-tooling|agent-api-unreachable|agent-api-after-verdict) return 0 ;;
        *) return 1 ;;
    esac
}

# Pure: map the QCI_GUI_RETRY knob to a MAX retry count (the number of ADDITIONAL
# fresh-VM attempts allowed after the first). Report-only when 0:
#   ''|0|off|false|no       -> 0   (report-only; record `would-retry`, do not re-run)
#   on|classified|true|yes  -> 1   (back-compat with the original boolean knob)
#   a bare non-negative int -> itself, capped at GUI_RETRY_CAP (runaway backstop)
#   anything else           -> 0   (fail safe: an unparseable knob never retries)
# Each retried attempt is still gated on the per-attempt classifier staying
# retriable, so a retry that surfaces a genuine product-fail stops the loop —
# the count is only the CEILING, never a guarantee of N reruns. Pure (args only)
# => host-testable (gui-retry-classify.bats).
GUI_RETRY_CAP=${GUI_RETRY_CAP:-5}
gui_retry_max() {
    case "$1" in
        ''|0|off|false|no) printf '0' ;;
        on|classified|true|yes) printf '1' ;;
        *)
            if [[ "$1" =~ ^[0-9]+$ ]]; then
                local n=$1
                [ "$n" -gt "$GUI_RETRY_CAP" ] && n=$GUI_RETRY_CAP
                printf '%s' "$n"
            else
                printf '0'
            fi ;;
    esac
}

# Host-side: does the agent log show an unambiguous qemu GUEST-AGENT CONNECTIVITY
# failure — the host transport could not reach the guest agent AT ALL? This is
# the ONLY discriminator that makes a timeout retriable. It deliberately does NOT
# match a generic `vm-exec ... timed out` / rc=124: vm-exec's own overall deadline
# (QDISTRO_VM_EXEC_TIMEOUT) fires on a wedged GUEST command, which is equally a
# PRODUCT hang — retrying that could flake-pass a real hang (codex). Only true
# agent-connectivity loss (libvirt/qemu-agent level) qualifies. Reads the log
# file; returns 0 when a connectivity-loss marker is present.
gui_detect_transport_marker() {
    local log_path=$1
    [ -f "$log_path" ] || return 1
    grep -qEi \
        'guest agent is not responding|qemu guest agent is not (connected|running)|guest-agent-not-responding|guest agent channel|agent unreachable|cannot connect to .*qemu.*agent' \
        "$log_path" 2>/dev/null
}

# Host-side: does the agent log show an unambiguous SHELL COMMAND-CONSTRUCTION
# error in the agent's OWN driver commands — i.e. the agent emitted a malformed
# `bash -c`/`sh -c` wrapper or an unterminated/unbalanced quoted string, so the
# command never validly ran? This is the discriminator that distinguishes
# "agent botched its own tooling and then declared FAIL" (NOT a trustworthy
# product result) from "agent ran the asserts and one genuinely failed".
#
# The marker set is deliberately TIGHT and ANCHORED: a matching line must BEGIN
# with a real shell-stderr prefix (`bash:`/`sh:`/`dash:`/`/bin/sh:` …, optional
# `[pid]`) AND carry one of a small set of parser/usage diagnostics. The anchor
# is load-bearing: the de-biased agent prompt now *teaches* phrases like
# "option requires an argument", so an agent merely quoting/discussing one in its
# narrative (or a product log echoing it) must NOT flip a genuine status=FAIL to
# retriable — only an actual shell emitting the diagnostic at the start of a line
# counts. It deliberately does NOT match broad patterns like `command not found`
# or a bare `No such file or directory`, which a real guest-side product/script
# problem can legitimately produce. Reads the log file; returns 0 when a
# command-construction marker is present.
# DID THE DRIVER ACTUALLY LOOK AT A FRAME? Echoes a count of image-open events
# found in the agent log.
#
# THIS IS A DIAGNOSTIC, NOT A GATE, and that is a deliberate choice. The trust
# model here is that the scenario driver is capable and honest; what the harness
# owes you is not a barrier but an ANSWER when a verdict looks wrong. "The agent
# opened 0 images and still graded a colour/absence claim" is the single fact
# that explains the largest class of bad visual verdicts seen in this project,
# and until now it was recoverable only by hand-grepping a 1000-line log.
#
# Why it must not be a gate: the pattern list below is driver-specific and will
# drift as tools are renamed, so a false zero would fail a scenario that was
# graded perfectly well. A wrong DIAGNOSTIC costs a confusing line in a report;
# a wrong GATE costs a red build and the trust of everyone reading it.
#
# What a 0 here means, concretely, from the runs on record: the overnight run
# full-20260914T194046Z graded 113 scenarios with ZERO image opens across all of
# them, while 75 of those logs MENTIONED an OCR binary the host did not have.
# Mentioned, not invoked: the count is
#   find <run>/gui -name '*.agent.log' -exec grep -l tesseract {} + | wc -l
# and a mention may be quoted instructions, so it bounds citations rather than
# counting invocations (75 by every spelling; an earlier comment said 74
# "invoked", which was both the wrong number and the wrong verb). A single-
# scenario rerun reproduced it (8 OCR invocations, 0 opens) and produced a FALSE
# FAIL on an absence assertion -- OCR cannot distinguish "the control is not
# there" from "I could not read this frame".
GUI_IMAGE_OPEN_PATTERNS=${QCI_IMAGE_OPEN_PATTERNS:-'view_image|image_view|read_image|"tool"[[:space:]]*:[[:space:]]*"Read"[^\n]*\.png|Read\([^)]*\.(png|jpg|jpeg|ppm)'}
# WHAT THIS ACTUALLY MEASURES -- read before trusting a number it produces.
#
# It counts image-open MENTIONS in the driver's own output. It does NOT count
# tool calls, because the sanctioned driver does not emit a machine-readable
# line when it opens an image: codex prints its narrative, not its tool
# invocations. So a positive count means the driver TALKED about opening a
# frame, which is weak evidence. A ZERO is the useful signal -- nothing in the
# driver's output claimed to look -- but it is a TREND signal, not proof: it
# over-counts negations and error prose ("could not use view_image"), and
# under-counts equivalent tool names, an open the driver never narrated, and any
# driver line that happens to equal a prompt line verbatim, which the
# subtraction below removes globally. Read it as "start here", never as a
# finding (sol, B round 2).
#
# The echoed prompt must be subtracted first. The scenario prompt itself
# contains the literal `view_image` (it is the instruction telling the driver
# to open the frame), and codex echoes its prompt into the log, so counting the
# raw log returned >= 1 on EVERY attempt by construction: the one reading this
# function was built to detect -- a lane-wide drop to zero -- was unreachable.
# Lines identical to prompt lines are therefore dropped before matching
# (fable, B round 1).
gui_count_image_opens() {
    local log_path=$1 prompt_path=${2:-} n=0
    [ -f "$log_path" ] || { printf '0\n'; return 0; }
    if [ -n "$prompt_path" ] && [ -f "$prompt_path" ]; then
        n=$(grep -vxF -f "$prompt_path" -- "$log_path" 2>/dev/null \
            | grep -cEi "$GUI_IMAGE_OPEN_PATTERNS") || n=0
    else
        n=$(grep -cEi "$GUI_IMAGE_OPEN_PATTERNS" "$log_path" 2>/dev/null) || n=0
    fi
    printf '%s\n' "${n:-0}"
}

gui_detect_agent_tooling_marker() {
    local log_path=$1
    [ -f "$log_path" ] || return 1
    grep -qE \
        '^[[:space:]]*(bash|sh|dash|/bin/sh|/bin/bash|/usr/bin/sh|/usr/bin/bash)(\[[0-9]+\])?:.*(-c: option requires an argument|unexpected EOF while looking for matching|syntax error near unexpected token|[Ss]yntax error: [Uu]nterminated quoted string)' \
        "$log_path" 2>/dev/null
}

# Host-side: does the agent log show an unambiguous LLM-PROVIDER failure? This is
# the discriminator for `agent-api-unreachable` and
# `agent-api-after-verdict`: a provider/infra outage, not a product failure. The
# marker is TIGHT and ANCHORED to exact agent CLI lines at the START
# of a line, so a product log or an agent narrative merely *mentioning* a
# connection error cannot flip a verdict. It deliberately does NOT match generic
# `timeout`/DNS/TLS/HTTP-5xx strings, which a real product or network scenario
# can legitimately produce; only the exact socket-level "Unable to connect to
# API (FailedToOpenSocket|ConnectionRefused)" family and the exact provider quota
# "You've hit your session limit" and Codex's exact selected-model-capacity lines
# qualify. Capacity is equally independent of guest/product state and is safe to
# retry with the caller-selected model. New provider reasons are widened
# DELIBERATELY here, never loosened in the correlation layer. Reads the log file;
# returns 0 when a provider-unreachable marker is present.
gui_detect_agent_api_marker() {
    local log_path=$1
    [ -f "$log_path" ] || return 1
    grep -qE \
        "^[[:space:]]*(API Error: Unable to connect to API \\((FailedToOpenSocket|ConnectionRefused)\\)[[:space:]]*|You've hit your session limit.*|ERROR: Selected model is at capacity\\. Please try a different model\\.[[:space:]]*)$" \
        "$log_path" 2>/dev/null
}

# Host-side: does the agent log show an unambiguous EXTERNAL-NETWORK fetch
# failure during scenario setup — a guest-side curl/zypper/registry download that
# could not reach an upstream CDN/mirror/registry (e.g. scenario 18 building the
# tier-2 weston-terminal image: `curl (56) Recv failure: Connection reset by
# peer` against the openSUSE CDN)? Such a failure is INFRA, not a product bug: an
# upstream outage must never read as a product-error. The marker set is anchored
# to real fetch-tool error formats (curl's `(NN)` exit form, curl/wget transport
# phrases, zypper download failures, and container-registry pull transport
# errors), so a product log merely mentioning "connection" cannot flip a genuine
# product ERROR. Because it only ever RECLASSIFIES an already-ERROR attempt from
# product-error to external-network (both remain failures — it changes the BUCKET,
# never flips to pass), a modestly broad pattern is acceptable. New external
# fetch-failure formats are widened DELIBERATELY here. Reads the log file; returns
# 0 when an external-network fetch-failure marker is present.
# A local / SLIRP / loopback / RFC1918 endpoint. Scenarios routinely curl the
# VM-local SLIRP host (10.0.2.2), loopback services (127.0.0.1/::1), and private
# services — a fetch failure against THOSE is a harness/vm-hostfwd or product bug,
# NOT an upstream outage, so it must stay ACTIONABLE. A leading boundary keeps
# e.g. "110.0.2.2" from matching the 10.x branch.
_QCI_LOCAL_HOST_RE='(localhost|::1|(^|[^0-9.])(127\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}|192\.168\.[0-9]{1,3}\.[0-9]{1,3}))'

gui_detect_external_network_marker() {
    local log_path=$1
    [ -f "$log_path" ] || return 1
    # (a) Inherently-EXTERNAL fetch failures: DNS name-resolution failures (a
    # numeric loopback/SLIRP/RFC1918 literal is never resolved, so "Could not
    # resolve host" only ever applies to a real external name) and zypper/mirror/
    # container-registry transport errors. These are external by construction.
    if grep -qEi \
        'Could not resolve host|Temporary failure in name resolution|Download \(curl\) error for|Error code: (Connection failed|Timeout)|Timeout exceeded when accessing|Curl error [0-9]+|(Download|Retrieving) .*(failed|timed out).*(mirror|repo|http)|Error: (initializing source|copying system image|pinging container registry|writing blob|reading blob|short read).*(timeout|refused|reset|no route|TLS handshake|unexpected EOF|i/o timeout)' \
        "$log_path" 2>/dev/null; then
        return 0
    fi
    # (b) Generic curl/wget TRANSPORT errors (restricted to transport curl exit
    # codes — couldn't-resolve/connect/timeout/SSL/recv/send, NOT HTTP-status
    # codes like 22 where the server DID answer). Count these as external ONLY
    # when the failing line does NOT reference a local/SLIRP/loopback/RFC1918
    # endpoint. A bare "curl: (56) Recv failure" with no host on the line is the
    # observed CDN-reset case and stays external; a "Failed to connect to 10.0.2.2"
    # is local and stays actionable.
    local candidates external
    candidates=$(grep -Ei \
        'curl: \((5|6|7|18|28|35|52|55|56)\)|wget: (unable to resolve|download timed out)|Recv failure: Connection reset by peer|Failed to connect to [^ ]+ port' \
        "$log_path" 2>/dev/null)
    [ -n "$candidates" ] || return 1
    external=$(printf '%s\n' "$candidates" | grep -Eiv "$_QCI_LOCAL_HOST_RE")
    [ -n "$external" ]
}

# Classifier-drift alarm (H6b). A FAILING attempt that matched NO infra/tooling
# marker (transport + agent-tooling + agent-api + external-network all 0) fell
# through to a generic product-*/no-verdict/timeout/unknown classifier. That is
# usually a real product signal — but it is ALSO exactly what happens when a
# provider/CLI message string DRIFTS and an infra failure is silently demoted to
# a product FAIL (the H6 concern). We cannot tell the two apart from the row
# alone, so preserve the raw evidence: copy the LAST ~20 lines of the agent log
# into a per-scenario sidecar under the artifact dir (never into the TSV — that
# would bloat the fixed-column contract + break H4 column validation). report.py
# counts these no-marker failing attempts so a RISING count is the drift alarm.
# Args: log_path sidecar_path. Best-effort; a missing log is a silent no-op.
gui_capture_unmatched_tail() {
    local log_path=$1 sidecar=$2
    [ -f "$log_path" ] || return 0
    {
        echo "# classifier-drift watch: failing attempt matched NO infra/tooling marker"
        echo "# (transport/agent-tooling/agent-api/external-network detectors all 0)"
        echo "# if an infra outage was silently demoted to product-fail, a drifted"
        echo "# marker string is likely in the tail below — compare against the detectors."
        echo "# --- last 20 lines of $(rel_path "$log_path") ---"
        tail -n 20 "$log_path" 2>/dev/null
    } >> "$sidecar"
}

# Detach every host-side GUI controller from the developer's desktop. The
# graphical system under test lives in the disposable VM; host processes only
# orchestrate libvirt, move evidence, and call a non-interactive visual model.
# Keep XDG_RUNTIME_DIR unchanged because qemu:///session's libvirt socket lives
# below it. WAYLAND_DISPLAY and the session-bus address are instead pointed at
# deliberately nonexistent endpoints; run_agent_command additionally hides the
# real socket files in a mount namespace.
gui_isolate_host_desktop() {
    export DISPLAY=
    export WAYLAND_DISPLAY=qci-host-display-disabled
    export DBUS_SESSION_BUS_ADDRESS=unix:path=/dev/null
    export XAUTHORITY=/dev/null
    export XDG_SESSION_TYPE=tty
    export XDG_ACTIVATION_TOKEN=
    export DESKTOP_STARTUP_ID=
    export QT_QPA_PLATFORM=offscreen
    export GDK_BACKEND=headless
    export SDL_VIDEODRIVER=dummy
    export BROWSER=/bin/false
    export SSH_ASKPASS=/bin/false
    export SSH_ASKPASS_REQUIRE=never
    export SUDO_ASKPASS=/bin/false
    export GIT_ASKPASS=/bin/false
    export NO_AT_BRIDGE=1
    export QCI_HOST_GUI_ISOLATED=1
}

# Populate an argv array with the mandatory host-desktop mount sandbox. The
# root filesystem remains writable because agents must write evidence and use
# repository VM helpers. Only desktop entry points are hidden: all X11 sockets,
# the live Wayland socket(s), and the user session bus. The libvirt sockets under
# XDG_RUNTIME_DIR/libvirt remain visible, so virsh qemu:///session still works.
gui_host_sandbox_args() {
    local -n out=$1
    local runtime=${XDG_RUNTIME_DIR:-/run/user/$(id -u)} host_socket
    command -v bwrap >/dev/null 2>&1 || return 127
    out=(bwrap --die-with-parent --dev-bind / /)
    if [ -d /tmp/.X11-unix ]; then
        out+=(--tmpfs /tmp/.X11-unix)
    fi
    for host_socket in "$runtime"/wayland-* "$runtime"/bus; do
        [ -S "$host_socket" ] || continue
        out+=(--ro-bind /dev/null "$host_socket")
    done
}

run_agent_command() {
    local prompt=$1 log_path=$2 cmd=${QCI_AGENT_CMD:-} expanded workdir rc
    local -a host_sandbox=()
    if [ -z "$cmd" ]; then
        return 127
    fi
    gui_isolate_host_desktop
    if ! gui_host_sandbox_args host_sandbox; then
        printf 'qci: bubblewrap is required to isolate GUI agents from the host desktop\n' > "$log_path"
        return 127
    fi
    # Agents occasionally invoke tools that treat an intended stdout formatter
    # (for example ImageMagick's `txt:-`) as a relative output filename. Running
    # from the source checkout then leaves that scratch artifact untracked at the
    # repository root. Give every attempt a private, disposable cwd under /tmp;
    # prompts and evidence paths are absolute, and repo access is through the
    # exported *_REPO variables, so no scenario contract depends on cwd.
    workdir=$(mktemp -d "${TMPDIR:-/tmp}/qci-agent.XXXXXX") || return 1
    # Host-side backstop timeout. QCI_AGENT_TIMEOUT (seconds) bounds the agent even
    # when the operator's QCI_AGENT_CMD does not self-wrap `timeout`; on expiry the
    # agent is killed (`timeout -k 15`, SIGTERM then SIGKILL after 15s) and the call
    # returns 124. gui_run_scenario then records a hard failure (rc=124 with no
    # status => fail closed). Default 0 = unbounded, preserving the historic behavior
    # where the operator's own command owns the budget (e.g. `timeout 720 claude`).
    # When BOTH are set the smaller deadline wins, so an operator's inner 720 still
    # fires first under a larger harness cap.
    local to=${QCI_AGENT_TIMEOUT:-0}
    [ "$to" -gt 0 ] 2>/dev/null || to=0
    (
        cd "$workdir" || exit 1
        if [[ "$cmd" == *"{prompt}"* ]]; then
            expanded=${cmd//\{prompt\}/$prompt}
            if [ "$to" -gt 0 ]; then
                timeout -k 15 "$to" "${host_sandbox[@]}" bash -lc "$expanded" < /dev/null > "$log_path" 2>&1
            else
                "${host_sandbox[@]}" bash -lc "$expanded" < /dev/null > "$log_path" 2>&1
            fi
        else
            if [ "$to" -gt 0 ]; then
                # shellcheck disable=SC2086
                timeout -k 15 "$to" "${host_sandbox[@]}" $cmd "$prompt" < /dev/null > "$log_path" 2>&1
            else
                # shellcheck disable=SC2086
                "${host_sandbox[@]}" $cmd "$prompt" < /dev/null > "$log_path" 2>&1
            fi
        fi
    )
    rc=$?
    if [ "$rc" -eq 0 ]; then
        rm -rf -- "$workdir" 2>/dev/null || true
        printf '\nqci_agent_workdir=%s (removed after success)\n' "$workdir" >> "$log_path"
    else
        printf '\nqci_agent_workdir=%s (preserved after agent exit %s)\n' \
            "$workdir" "$rc" >> "$log_path"
    fi
    return "$rc"
}

gate_qdshell_ui_agent() {
    # Runs the qdshell agent-assisted UI vision pytest. The harness drives the
    # LIVE qdshell session inside the qdwin VM acquired by gate_gui (IPC over
    # wayland-1 via vm-exec, screenshots via qdwin's in-compositor
    # shell-authorized capture — virsh only sees the tty console), because the
    # host headless nested compositor SIGSEGVs quickshell during early
    # FileView settings load (see
    # todo/qdwin-vm/agent-ui-harness-headless-quickshell-crash.md). codex
    # describe/judge still run on the host against the pulled-back PNGs.
    local vm=${1:-}
    local rc=$EXIT_OK cmd
    if [ -z "$vm" ]; then
        # No VM in scope — do NOT silently fall through to the crashing host
        # path. Mark the gate as failed with a precise reason.
        record_result gui qdshell-ui fail "$EXIT_VISUAL" "$(exit_class_name "$EXIT_VISUAL")" vision "" \
            "no GUI VM passed to gate_qdshell_ui_agent; cannot reach a live qdshell session"
        return "$EXIT_VISUAL"
    fi
    # Pass the VM + the exact transport tools down to the pytest harness.
    # QDSHELL_UI_VM switches runner.py to the VM transport; the harness
    # validates the domain name and refuses to drive any non-allowlisted
    # IPC token, and base64-wraps every guest command (no sh -c injection).
    # Each value is shell-quoted with printf %q so a name/path containing
    # spaces or quotes cannot break out of the run_logged `bash -lc` string.
    local q_vm q_exec q_virsh
    q_vm=$(printf '%q' "$vm")
    q_exec=$(printf '%q' "$VM_TOOLS/vm-exec")
    q_virsh=$(printf '%q' "${VIRSH[*]}")
    cmd="QDSHELL_UI_TESTS=1 \
QDSHELL_UI_VM=$q_vm \
QDSHELL_UI_VM_EXEC=$q_exec \
QDSHELL_UI_VIRSH=$q_virsh \
python3 -m pytest tests/ui -v"
    run_logged gui qdshell-ui "$EXIT_VISUAL" vision "$WORKSPACE/qdshell" "$cmd" "qdshell agent-assisted UI pytest (VM $vm)" || rc=$?
    if [ -d "$WORKSPACE/qdshell/tests/ui/artifacts" ]; then
        mkdir -p "$RDIR/gui/qdshell-ui-artifacts"
        cp -a "$WORKSPACE/qdshell/tests/ui/artifacts/." "$RDIR/gui/qdshell-ui-artifacts/" 2>/dev/null || true
    fi
    return "$rc"
}

# How many GUI scenarios to run concurrently. GUI VMs are heavier than bats
# (nested KVM + compositor) and each spawns its own agent (QCI_AGENT_CMD)
# process. The default is deliberately serial: running many full GUI stacks at
# once has repeatedly produced black screenshots, missed input/focus events, and
# agent timeouts that do not reproduce in isolation. QCI_GUI_JOBS remains an
# explicit opt-in for throughput experiments.
gui_job_count() {
    local jobs ram_gb ram_cap
    if [ -n "${QCI_GUI_JOBS:-}" ] && [ "${QCI_GUI_JOBS}" -ge 1 ] 2>/dev/null; then
        jobs=$QCI_GUI_JOBS
    else
        jobs=1
    fi
    # Clamp by current MemAvailable (reclaimable cache included), not MemTotal.
    ram_gb=$(awk '/^MemAvailable:/{printf "%d", $2/1024/1024}' /proc/meminfo 2>/dev/null)
    [ -n "$ram_gb" ] && [ "$ram_gb" -gt 0 ] 2>/dev/null \
        || ram_gb=$(awk '/^MemTotal:/{printf "%d", $2/1024/1024}' /proc/meminfo 2>/dev/null)
    [ -n "$ram_gb" ] 2>/dev/null || ram_gb=8
    ram_cap=$(( (ram_gb - 6) / 5 ))
    [ "$ram_cap" -lt 1 ] && ram_cap=1
    [ "$jobs" -gt "$ram_cap" ] && jobs=$ram_cap
    [ "$jobs" -lt 1 ] && jobs=1
    printf '%s\n' "$jobs"
}

# Run one GUI agent scenario on its own fresh disposable VM, record the result +
# timing, then release the VM. Self-contained for backgrounded pool execution.
# Returns 0 on pass/skip, EXIT_GUI on failure, EXIT_VM_PROVISION if no VM.
gui_run_scenario() {
    local scenario=$1 provided=${2:-} rel vm prompt log_path status agent_rc frc=0 own=0 vm_live=1 t0 t1 t2 ta0 ta1 gate_name lane adir
    rel=$(gui_scenario_rel "$scenario")
    # Scheduling lane for the attempt ledger + correlated-burst detector: a qdwin
    # scenario runs on the heavier gui-qdwin profile, everything else on gui-admin.
    if gui_scenario_requires_qdwin "$rel"; then lane=qdwin; else lane=admin; fi
    t0=$(date +%s)
    if [ -n "$provided" ]; then
        vm=$provided
    else
        if gui_scenario_requires_qdwin "$rel"; then
            gate_name="gui-qdwin-$(safe_name "$rel")"
        else
            gate_name="gui-admin-$(safe_name "$rel")"
        fi
        vm=$(acquire_vm "$gate_name" "") || {
            record_result gui "$rel" fail "$EXIT_VM_PROVISION" vm_provision vm "" "GUI VM creation failed"
            record_timing gui "$rel" "$(( $(date +%s) - t0 ))" 0 "$(( $(date +%s) - t0 ))" provfail ""
            return "$EXIT_VM_PROVISION"
        }
        own=1
    fi
    t1=$(date +%s)
    local slug scratch art_alias caplog
    slug=$(safe_name "$rel")
    adir="$RDIR/gui/$slug"
    prompt="$RDIR/agent-notes/$slug.prompt.md"
    log_path="$RDIR/gui/$slug.agent.log"
    mkdir -p "$(dirname "$log_path")" "$adir"
    # Short real /tmp directory for weak agents that truncate long paths and for
    # ImageMagick policies that reject symlinked output paths. Harvest copies it
    # into the canonical run directory after the agent exits.
    art_alias=$(gui_make_artifact_alias "$adir") || art_alias=$adir
    # Per-scenario isolated scratch dir (host) + slug (for guest scratch on a
    # shared session VM). Passed to the agent's env at run_agent_command so a
    # scenario routes scratch here instead of a collision-prone fixed /tmp path.
    scratch=$(scenario_scratch_dir gui "$slug")
    mkdir -p "$scratch"
    # HARNESS CAPTURE LOG for this attempt. Deliberately in the run tree, NOT in
    # the artifact dir and NOT in the agent's scratch: it is the ledger the gate
    # reads to learn what it actually captured, and the prompt never mentions it.
    caplog="$RDIR/gui/captures/$slug.tsv"
    # BOUND to this attempt's VM: a capture of any other worker's VM is refused
    # at the producer and rejected at the verifier.
    gui_capture_log_init "$caplog" "$vm" || caplog=""
    write_agent_prompt "$vm" "$scenario" "$prompt" "$art_alias" "$scratch" "$slug"
    # Deliver the guest waiter library so the scenario can source
    # /tmp/qci-gui-waiters.sh (best-effort; a scenario that needs it and lacks it
    # fails its own assertion loudly).
    install_gui_waiters "$vm" || log "agent scenario $rel: waiter-lib delivery failed (continuing)"
    suppress_idle_lock "$vm"
    log "agent scenario $rel on $vm"
    record_host_load gui "$rel" start
    ta0=$(date +%s)
    # Export VMNAME so the scenario's `VM=${VMNAME:?...}` always resolves to the
    # right disposable VM deterministically, instead of relying on the agent to
    # set it from the prompt (or a racy `virsh list | head` fallback).
    # QCI_GUI_ARTIFACT_DIR is the SHORT alias (preferred for agents); harvest
    # still grades the canonical adir after recovery.
    VMNAME="$vm" QCI_SCENARIO_TMPDIR="$scratch" QCI_SCENARIO_SLUG="$slug" \
        QCI_GUI_ARTIFACT_DIR="$art_alias" QCI_GUI_CAPTURE_LOG="$caplog" \
        run_agent_command "$prompt" "$log_path"
    agent_rc=$?
    ta1=$(date +%s)
    record_host_load gui "$rel" end
    gui_harvest_agent_artifacts "$adir" "$slug" "$log_path" "$art_alias"
    # SEAL the ledger now that the agent process is gone. `capanchor` lives only
    # in this shell; it is what makes every later edit (tail truncation
    # included) detectable. An empty anchor is fail-closed at grade time.
    local capanchor=""
    if [ -n "$caplog" ]; then
        capanchor=$(gui_capture_log_seal "$caplog" "$vm") || capanchor=""
        if [ -n "$capanchor" ]; then
            capanchor="$vm"$'\t'"$capanchor"
        else
            log "agent scenario $rel: could not seal the capture ledger; the visual verdict will not be graded"
        fi
    fi
    status=$(agent_artifact_status "$adir" "$log_path")
    # VISUAL-EVIDENCE CONTRACT — harness-attested, between harvest and the
    # verdict mapping. The GATE now runs OCR itself over the frames this
    # scenario harvested; a pixel-dependent scenario's PASS/FAIL survives only
    # when the harness could read those frames. Nothing the agent wrote is
    # accepted as evidence, so artifact ordering is irrelevant here.
    local ev_note=""
    IFS=$'\t' read -r status ev_note < <(gui_apply_visual_evidence_contract \
        "$status" "$scenario" "$adir" "$caplog" "$capanchor")
    # Fail-closed status/rc mapping (see gui_agent_verdict). UNKNOWN:0 — an agent
    # that exited 0 without rendering a usable verdict — is a hard failure here,
    # not the silent pass it used to be.
    local verdict note skip_why
    IFS=$'\t' read -r verdict note < <(gui_agent_verdict "$status" "$agent_rc")
    # A skip's REASON is the whole value of the row; without it every skipped
    # scenario reads the same and a missing golden dependency is invisible.
    if [ "$verdict" = skip ]; then
        skip_why=$(gui_skip_reason "$adir")
        [ -n "$skip_why" ] && note="$note: $skip_why"
    fi
    if [ -n "$ev_note" ]; then
        note="$note; $ev_note"
        printf '\nqci_gui_visual_evidence: %s\n' "$ev_note" >> "$log_path" 2>/dev/null || true
    fi
    # DIAGNOSTIC, never a gate (see gui_count_image_opens). Recorded for EVERY
    # attempt so the report can answer "did the driver look?" without anyone
    # grepping a thousand-line log, and so a lane-wide drop to zero -- the shape
    # of the 113-scenario run that started this workstream -- is visible as a
    # trend instead of being rediscovered by hand.
    local img_opens
    img_opens=$(gui_count_image_opens "$log_path" "$prompt")
    printf '\nqci_gui_image_opens: %s\n' "$img_opens" >> "$log_path" 2>/dev/null || true
    if [ "$(gui_scenario_visual_mode "$scenario")" = required ] && [ "$img_opens" -eq 0 ]; then
        note="$note; DIAGNOSTIC: the driver never MENTIONED opening an image for a pixel-dependent scenario (verdict NOT changed; if this verdict is wrong, start here)"
    fi
    # Classify a failing attempt (mechanical signature only) for the attempt
    # ledger + the retry decision. Empty for pass/skip.
    local classifier="" transport=0 tooling=0 api=0 extnet=0
    # Preserve the FIRST attempt's rc: the retry note + flake ledger below must
    # report the real cause, not a hard-coded 124. agent-tooling fails typically
    # carry rc=1, not the 124 that the transport-timeout path assumed.
    local agent_rc1=$agent_rc
    if [ "$verdict" = fail ]; then
        gui_detect_transport_marker "$log_path" && transport=1
        gui_detect_agent_tooling_marker "$log_path" && tooling=1
        gui_detect_agent_api_marker "$log_path" && api=1
        gui_detect_external_network_marker "$log_path" && extnet=1
        classifier=$(gui_classify_failure "$status" "$agent_rc" "$transport" "$tooling" "$api" "$extnet")
        # Classifier-drift alarm (H6b): a fail with NO marker matched fell through
        # to a generic classifier — snapshot the log tail so a drifted infra
        # marker is not lost. report.py counts these rows (rising count => drift).
        if [ "$((transport + tooling + api + extnet))" -eq 0 ]; then
            gui_capture_unmatched_tail "$log_path" "${log_path%.agent.log}.unmatched-tail.txt"
        fi
    fi
    # Per-attempt observability row: the RAW agent status + rc + wall seconds +
    # classifier, before the verdict collapses it. This is where the flake signal
    # lives (rc=124, UNKNOWN, slow walls under load).
    record_attempt gui "$rel" 1 "$status" "$agent_rc" "$classifier" "$((ta1 - ta0))" "$vm" "$log_path" "$ta0" "$ta1" "$lane"

    # Classified retry (DEFAULT OFF = report-only). A failing attempt with a
    # retriable signature (transport-timeout, agent-tooling,
    # agent-api-unreachable, or agent-api-after-verdict — agent-timeout and any
    # product-fail/error are excluded as masking risks)
    # either records a
    # `would-retry` flake row (report-only) or, when QCI_GUI_RETRY enables it AND
    # this is a disposable (own) VM, runs UP TO N more attempts on FRESH VMs
    # (N = gui_retry_max). The loop re-classifies after EACH attempt and stops the
    # moment a verdict is no longer fail-and-retriable: a retry that surfaces a
    # genuine product-fail is adopted immediately and never re-rolled, so extra
    # retries can never flake-pass a real product bug. Every retry emits an
    # attempt row, and a retried run always emits a flake.tsv row + a note on the
    # result, so a retry can never silently turn a flake green.
    local retry_max
    retry_max=$(gui_retry_max "${QCI_GUI_RETRY:-0}")
    if [ "$verdict" = fail ] && [ "$own" = 1 ] && gui_classifier_retriable "$classifier"; then
        if [ "$retry_max" -ge 1 ]; then
            # Snapshot the FIRST attempt's evidence — the basis for the retriable
            # classification and the audit trail that makes retry acceptable.
            local log_path_base=$log_path first_classifier=$classifier first_status=$status
            local attempt=0 provision_failed=0
            while [ "$attempt" -lt "$retry_max" ] \
                  && [ "$verdict" = fail ] \
                  && gui_classifier_retriable "$classifier"; do
                attempt=$((attempt + 1))
                local ordinal=$((attempt + 1))   # attempt-2 is the first retry
                log "agent scenario $rel: retriable signature ($classifier); retry $attempt/$retry_max on a fresh VM"
                collect_vm_artifacts "$vm" "gui-$(safe_name "$rel")"
                release_vm "$vm" "$EXIT_GUI"
                vm_live=0   # previous VM collected+released; nothing live until a fresh one is up
                # Each retry writes to its OWN log + artifact dir so every attempt's
                # evidence is preserved and each fresh agent starts clean.
                local vmN logN adirN scratchN art_aliasN tsa tsb statusN verdictN noteN classifierN transportN toolingN apiN caplogN_vm
                logN="${log_path_base%.agent.log}.retry${attempt}.agent.log"
                adirN="${adir%.retry*}.retry${attempt}"
                mkdir -p "$adirN"
                art_aliasN=$(gui_make_artifact_alias "$adirN") || art_aliasN=$adirN
                # Fresh host scratch PER RETRY so stale scratch from the failed
                # attempt can't leak into the retry (mirrors the per-attempt
                # logN/adirN discipline).
                scratchN=$(scenario_scratch_dir gui "${slug}-retry${attempt}")
                mkdir -p "$scratchN"
                # Fresh capture log per retry, next to the fresh adirN/logN.
                local caplogN="$RDIR/gui/captures/${slug}-retry${attempt}.tsv"
                caplogN_vm=""        # bound once the retry VM is acquired
                vmN=$(acquire_vm "$gate_name" "")
                if [ -z "$vmN" ]; then
                    log "agent scenario $rel: retry $attempt VM provision failed; keeping the previous verdict"
                    record_flake "$rel" "$first_classifier" "$first_status" "$agent_rc1" "" "$ordinal" retry-vm-provision-failed "$log_path_base"
                    provision_failed=1
                    break
                fi
                vm=$vmN; vm_live=1
                caplogN_vm=$vmN
                gui_capture_log_init "$caplogN" "$vmN" || caplogN=""
                write_agent_prompt "$vmN" "$scenario" "$prompt" "$art_aliasN" "$scratchN" "$slug"
                install_gui_waiters "$vmN" || log "agent scenario $rel: waiter-lib delivery failed (continuing)"
                suppress_idle_lock "$vmN"
                record_host_load gui "$rel" start
                tsa=$(date +%s)
                VMNAME="$vmN" QCI_SCENARIO_TMPDIR="$scratchN" QCI_SCENARIO_SLUG="$slug" \
                    QCI_GUI_ARTIFACT_DIR="$art_aliasN" QCI_GUI_CAPTURE_LOG="$caplogN" \
                    run_agent_command "$prompt" "$logN"
                agent_rc=$?; tsb=$(date +%s)
                record_host_load gui "$rel" end
                gui_harvest_agent_artifacts "$adirN" "$slug" "$logN" "$art_aliasN"
                local capanchorN=""
                if [ -n "$caplogN" ]; then
                    capanchorN=$(gui_capture_log_seal "$caplogN" "$caplogN_vm") || capanchorN=""
                    if [ -n "$capanchorN" ]; then
                        capanchorN="$caplogN_vm"$'\t'"$capanchorN"
                    fi
                fi
                statusN=$(agent_artifact_status "$adirN" "$logN")
                local ev_noteN=""
                IFS=$'\t' read -r statusN ev_noteN < <(gui_apply_visual_evidence_contract \
                    "$statusN" "$scenario" "$adirN" "$caplogN" "$capanchorN")
                transportN=0; toolingN=0; apiN=0; classifierN=""; local extnetN=0
                IFS=$'\t' read -r verdictN noteN < <(gui_agent_verdict "$statusN" "$agent_rc")
                if [ "$verdictN" = skip ]; then
                    skip_why=$(gui_skip_reason "$adirN")
                    [ -n "$skip_why" ] && noteN="$noteN: $skip_why"
                fi
                if [ -n "$ev_noteN" ]; then
                    noteN="$noteN; $ev_noteN"
                    printf '\nqci_gui_visual_evidence: %s\n' "$ev_noteN" >> "$logN" 2>/dev/null || true
                fi
                if [ "$verdictN" = fail ]; then
                    gui_detect_transport_marker "$logN" && transportN=1
                    gui_detect_agent_tooling_marker "$logN" && toolingN=1
                    gui_detect_agent_api_marker "$logN" && apiN=1
                    gui_detect_external_network_marker "$logN" && extnetN=1
                    classifierN=$(gui_classify_failure "$statusN" "$agent_rc" "$transportN" "$toolingN" "$apiN" "$extnetN")
                    if [ "$((transportN + toolingN + apiN + extnetN))" -eq 0 ]; then
                        gui_capture_unmatched_tail "$logN" "${logN%.agent.log}.unmatched-tail.txt"
                    fi
                fi
                record_attempt gui "$rel" "$ordinal" "$statusN" "$agent_rc" "$classifierN" "$((tsb - tsa))" "$vmN" "$logN" "$tsa" "$tsb" "$lane"
                # Promote this attempt as the new current state; the loop guard
                # re-evaluates verdict+classifier to decide whether to keep going.
                # Same DIAGNOSTIC as the first attempt. It used to be recorded
                # only on attempt 1 while the comment claimed every attempt, so
                # a retried scenario -- exactly the kind whose verdict is most
                # often wrong -- carried no answer to "did the driver look?".
                local img_opensN
                img_opensN=$(gui_count_image_opens "$logN" "$prompt")
                printf '\nqci_gui_image_opens: %s\n' "$img_opensN" >> "$logN" 2>/dev/null || true
                if [ "$(gui_scenario_visual_mode "$scenario")" = required ] && [ "$img_opensN" -eq 0 ]; then
                    noteN="$noteN; DIAGNOSTIC: the driver never MENTIONED opening an image for a pixel-dependent scenario (verdict NOT changed; if this verdict is wrong, start here)"
                fi
                status=$statusN; verdict=$verdictN; note=$noteN; classifier=$classifierN; log_path=$logN; adir=$adirN
            done
            # Summarize the retried run (skip when we bailed on a provision failure,
            # which already recorded its own flake row).
            if [ "$attempt" -ge 1 ] && [ "$provision_failed" = 0 ]; then
                local total=$((attempt + 1))
                if [ "$verdict" = fail ]; then
                    note="classified retry exhausted: first_classifier=$first_classifier first_rc=$agent_rc1 attempts=$total; final: $note"
                    record_flake "$rel" "$first_classifier" "$first_status" "$agent_rc1" "$status" "$total" retried-fail "$log_path_base"
                else
                    note="classified flake: classifier=$first_classifier first_rc=$agent_rc1 attempts=$total; $note"
                    record_flake "$rel" "$first_classifier" "$first_status" "$agent_rc1" "$status" "$total" retried-pass "$log_path_base"
                fi
            fi
        else
            # Report-only (default): record what WOULD be retried, do not re-run.
            record_flake "$rel" "$classifier" "$status" "$agent_rc" "" 1 would-retry "$log_path"
        fi
    fi

    # Final result from the (possibly retried) verdict.
    case "$verdict" in
        pass) record_result gui "$rel" pass 0 pass agent "$log_path" "$note" ;;
        skip) record_result gui "$rel" skip 0 pass agent "$log_path" "$note" ;;
        *)    local fail_note=$note
              # An external-network fetch failure during setup is infra, not a
              # product regression: tag the result note with a stable marker the
              # report keys on (nonactionable_failure_reason) so an upstream CDN/
              # registry outage is bucketed non-actionable, not counted as a
              # product failure. The row is still surfaced (Expected/non-actionable
              # section), never hidden.
              if [ "$classifier" = external-network ]; then
                  fail_note="external-network infra: $note (guest fetch/registry failure during setup — upstream outage, not a product failure)"
              fi
              record_result gui "$rel" fail "$EXIT_GUI" gui agent "$log_path" "$fail_note"
              frc=$EXIT_GUI ;;
    esac
    t2=$(date +%s)
    if [ "$vm_live" = 1 ]; then
        collect_vm_artifacts "$vm" "gui-$(safe_name "$rel")"
        [ "$own" = 1 ] && release_vm "$vm" "$frc"
    fi
    record_timing gui "$rel" "$((t1 - t0))" "$((t2 - t1))" "$((t2 - t0))" "$frc" "$vm"
    return "$frc"
}

# Decide, purely from the session-VM capability flags, whether a GUI agent
# scenario must be SKIPPED because the OUTER stack it needs is not provisioned
# in this VM profile (rather than dispatched to the agent, which would then
# write ERROR — the bug this fixes). Mirrors the bats `tiered-isolation` skip.
#
# Critical SKIP-vs-ERROR boundary (see scenarios 20/56 Setup notes): SKIP only
# when the tier-4/5 OUTER stack itself is unprovisioned — no qdwin/qdshell
# compositor on wayland-1, or no nested KVM (/dev/kvm). When that outer stack IS
# present but only the baked guest image (qdistro-tier{4,5}-*.qcow2) is missing
# or broken, this is a PRESENT-BUT-BROKEN bake: the scenario MUST run and the
# agent reports ERROR/INFRA per its own contract ("do not silently skip"). So
# image presence is deliberately NOT part of the skip decision — the agent
# evaluates the image and emits ERROR/INFRA when the provisioned bake is broken.
#
# Pure function: reads ONLY its arguments (no globals, no VM I/O), so the
# verdict logic is host-testable without the GUI VM stack — see
# tests/integration/qci/gui-scenario-skip.bats. Echoes the human-readable skip reason
# when the scenario must be skipped, or nothing when it should run.
#
# Tier-4/5 base images are OPT-IN (built only under QDISTRO_BUILD_TIER{4,5}_BASE
# =1). In the default lane the outer stack is present but the image is
# intentionally absent — dispatching every run to the agent just to get ERROR is
# noise. So: present-stack-but-absent-image is a clean SKIP *unless the run opted
# in*, in which case an absent/broken bake runs and the agent reports ERROR (the
# build was requested). This is decided HERE, separately from
# gui_scenario_skip_reason, because it must run BEFORE the qdwin-routing bypass
# in the dispatch loop (tier-4/5 scenarios are qdwin-required, so the bypass
# would otherwise skip the stack-presence function entirely). Pure function:
# host-testable. Echoes the skip reason, or nothing when the scenario should run.
#
# Args: rel tier5_base_present tier4_base_present tier5_optin tier4_optin
gui_scenario_tier_base_skip_reason() {
    local rel=$1 tier5_base=${2:-1} tier4_base=${3:-1} tier5_optin=${4:-0} tier4_optin=${5:-0}
    case "$rel" in
        qdistro/tests/integration/permissions-gui/20-tier5-vm-cold-start.md|\
        qdistro/tests/integration/permissions-gui/21-tier5-close-cleanup.md)
            [ "$tier5_base" != 1 ] && [ "$tier5_optin" != 1 ] && \
                printf '%s\n' "tier-5 base image not built (opt-in: QDISTRO_BUILD_TIER5_BASE=1 on a nested-KVM host)" ;;
        qdistro/tests/integration/permissions-gui/56-tier4-rdp-window-visible.md|\
        qdistro/tests/integration/permissions-gui/57-tier4-rdp-close-cleanup.md)
            [ "$tier4_base" != 1 ] && [ "$tier4_optin" != 1 ] && \
                printf '%s\n' "tier-4 base image not built (opt-in: QDISTRO_BUILD_TIER4_BASE=1 on a nested-KVM host)" ;;
    esac
    return 0
}

# qdwin app-compatibility scenarios (qdwin/tests/apps/*.md) drive real desktop
# apps (foot/xterm/gnome-text-editor/...) that are only installed when the golden
# was built with QDWIN_APP_DEPS=1 (fresh-vm-bootstrap.sh §app-deps lane). The
# default full-run golden is lean (QDWIN_APP_DEPS=0), so these scenarios have no
# apps to exercise. Dispatching them to the agent anyway is exactly what produced
# the run's fail-closed UNKNOWN (apps/04): the agent CORRECTLY judged SKIP but its
# machine-readable verdict was not captured, so the row failed closed. Decide the
# capability deterministically HERE — before the agent starts — so a golden that
# lacks app deps yields a clean SKIP naming the missing capability, with no
# reliance on the agent writing a verdict. Like the tier-base gate, this must run
# BEFORE the qdwin-routing bypass in the dispatch loop (app scenarios are
# qdwin-required). Pure (reads only its args) => host-testable. Echoes the skip
# reason, or nothing when the scenario should run.
#
# Args: rel app_deps
gui_scenario_app_deps_skip_reason() {
    local rel=$1 app_deps=${2:-0}
    case "$rel" in
        qdwin/tests/apps/[0-9][0-9]-*.md)
            [ "$app_deps" != 1 ] && \
                printf '%s\n' "qdwin app-test deps not installed (golden built with QDWIN_APP_DEPS=0); rebuild with QDWIN_APP_DEPS=1 for the app-compatibility lane"
            ;;
    esac
    return 0
}

# qterminal/Textual scenarios traverse the legacy XWayland presentation and
# focus path. They are useful periodic desktop-integration coverage, but are
# not part of the supported blocking GUI surface: virsh framebuffer captures
# and X11 focus are independently flaky on the labwc template. Keep the
# scenarios available in an explicit opt-in lane rather than deleting them.
#
# Args: rel xwayland_optin
gui_scenario_xwayland_skip_reason() {
    local rel=$1 xwayland_optin=${2:-0}
    [ "$xwayland_optin" = 1 ] && return 0
    case "$rel" in
        qdistro/tests/integration/permissions-gui/01-tui-approver-visual.md|\
        qdistro/tests/integration/permissions-gui/02-tui-scope-picker.md|\
        qdistro/tests/integration/permissions-gui/05-tui-help-overlay.md|\
        qdistro/tests/integration/permissions-gui/09-tui-broker-offline.md|\
        qdistro/tests/integration/permissions-gui/35-tui-and-qt-concurrent.md|\
        qdistro/tests/integration/permissions-gui/40-tui-survives-broker-restart.md|\
        qdistro/tests/integration/permissions-gui/48-qsu-tui-argv-rendering.md)
            printf '%s\n' "XWayland/qterminal E2E is opt-in (set QCI_XWAYLAND_E2E=1 for the dedicated desktop-integration lane)" ;;
    esac
    return 0
}

# Tier-4/5 base-image opt-in skip is handled SEPARATELY by
# gui_scenario_tier_base_skip_reason (above) so it can run BEFORE the
# qdwin-routing bypass in the dispatch loop; this function stays purely about
# OUTER-stack presence.
#
# Args: rel legacy_ctrl nested_kvm qdshell_active vm_ssh_port skip_qdwin
gui_scenario_skip_reason() {
    local rel=$1 legacy_ctrl=$2 nested_kvm=$3 qdshell_active=$4 vm_ssh_port=$5 skip_qdwin=${6:-0}
    if [ "$skip_qdwin" = 1 ] && gui_scenario_requires_qdwin "$rel"; then
        printf '%s\n' "QCI_GUI_SKIP_QDWIN=1: qdwin-dependent scenario skipped"
        return 0
    fi
    case "$rel" in
        qdwin/tests/gui/[0-9][0-9]-*.md)
            [ "$legacy_ctrl" != 1 ] && \
                printf '%s\n' "legacy qdshell ctrl-socket not available" ;;
        qdistro/tests/integration/permissions-gui/20-tier5-vm-cold-start.md|\
        qdistro/tests/integration/permissions-gui/21-tier5-close-cleanup.md)
            # Tier-5 OUTER stack: the qdwin/qdshell compositor on wayland-1 +
            # nested KVM. Absent => the opt-in tier-5 bake is not provisioned at
            # all => SKIP. (A present outer stack with only the base image
            # missing is broken-not-absent and runs => agent ERROR.)
            if [ "$qdshell_active" != 1 ]; then
                printf '%s\n' "tier-5 outer stack not provisioned: qdwin/qdshell session (wayland-1) absent in this VM profile"
            elif [ "$nested_kvm" != 1 ]; then
                printf '%s\n' "tier-5 outer stack not provisioned: nested KVM (/dev/kvm) absent in this VM"
            fi ;;
        qdistro/tests/integration/permissions-gui/56-tier4-rdp-window-visible.md|\
        qdistro/tests/integration/permissions-gui/57-tier4-rdp-close-cleanup.md)
            # Tier-4 OUTER stack: the qdwin/qdshell compositor on wayland-1 +
            # nested KVM. Absent => the opt-in tier-4 bake is not provisioned at
            # all => SKIP. (A present outer stack with only the guest image
            # missing is broken-not-absent and runs => agent ERROR/INFRA.)
            if [ "$qdshell_active" != 1 ]; then
                printf '%s\n' "tier-4 outer stack not provisioned: qdwin/qdshell session (wayland-1) absent in this VM profile"
            elif [ "$nested_kvm" != 1 ]; then
                printf '%s\n' "tier-4 outer stack not provisioned: nested KVM (/dev/kvm) absent in this VM"
            fi ;;
        qdistro/tests/integration/permissions-gui/18-podapps-launcher-badge.md|\
        qdistro/tests/integration/permissions-gui/19-tier5-loopback-visible.md)
            [ "$qdshell_active" != 1 ] && \
                printf '%s\n' "qdshell session not active in this VM profile" ;;
        qdistro/tests/integration/qdwin-noctalia/[0-9][0-9]-*.md)
            [ "$qdshell_active" != 1 ] && \
                printf '%s\n' "qdshell session not active in this VM profile" ;;
        qdlocker/tests/gui/[0-9][0-9]-*.md)
            [ "$qdshell_active" != 1 ] && \
                printf '%s\n' "qdshell session not active in this VM profile" ;;
        qdistro/tests/integration/permissions-gui/55-qsu-selinux-enforcing.md)
            [ -z "$vm_ssh_port" ] && \
                printf '%s\n' "VM_SSH_PORT not set for SSH-only SELinux scenario" ;;
    esac
    # Always succeed: a no-skip outcome (empty stdout) must not look like a
    # failure to callers. Without this the trailing `[ ] && printf` short-circuit
    # would leak a nonzero status when the scenario should run.
    return 0
}

# Pure preflight capability summary (Phase 3 observability). Given the SAME
# session-VM capability flags the dispatch loop already probed, report which
# SHARED preconditions are absent and which whole lanes that takes down — so a
# profile gap (e.g. no qdshell session => EVERY qdshell/noctalia/qdlocker/podapps
# scenario skips) is visible ONCE, up front, instead of being inferred from N
# scattered skip rows. This deliberately does NOT re-derive per-scenario
# decisions (that stays the single source of truth in gui_scenario_skip_reason /
# gui_scenario_tier_base_skip_reason); it only summarizes the same inputs. It is
# reporting only — it changes no dispatch decision and fails nothing. Echoes one
# observation per line, empty when the profile is fully capable. Pure (reads only
# its args) => host-testable (tests/integration/qci/gui-preflight.bats).
# Args: skip_qdwin qdshell_active nested_kvm legacy_ctrl vm_ssh_port \
#       tier5_base tier4_base tier5_optin tier4_optin
gui_preflight_capabilities() {
    local skip_qdwin=$1 qdshell_active=$2 nested_kvm=$3 legacy_ctrl=$4 vm_ssh_port=$5
    local tier5_base=${6:-0} tier4_base=${7:-0} tier5_optin=${8:-0} tier4_optin=${9:-0}
    if [ "$skip_qdwin" = 1 ]; then
        printf '%s\n' "qdwin lane DISABLED (QCI_GUI_SKIP_QDWIN=1): qdwin/qdshell/noctalia/qdlocker/tier scenarios skip"
    else
        [ "$qdshell_active" != 1 ] && \
            printf '%s\n' "qdshell session ABSENT on wayland-1: qdshell/noctalia/qdlocker/podapps scenarios will skip (profile gap, not per-scenario failures)"
        [ "$nested_kvm" != 1 ] && \
            printf '%s\n' "nested KVM (/dev/kvm) ABSENT: tier-4/5 cold-start/cleanup scenarios will skip"
    fi
    [ -z "$vm_ssh_port" ] && \
        printf '%s\n' "VM_SSH_PORT unset: the SSH-only SELinux-enforcing scenario (55) will skip"
    # Tier base images are only noteworthy when the run OPTED IN but the bake is
    # absent: a REQUESTED-but-missing environment runs and the agent reports
    # ERROR (a broken requested bake is a real failure, never a silent skip).
    [ "$tier5_optin" = 1 ] && [ "$tier5_base" != 1 ] && \
        printf '%s\n' "tier-5 base image REQUESTED (QDISTRO_BUILD_TIER5_BASE=1) but ABSENT: scenarios 20/21 will run and ERROR on the broken bake"
    [ "$tier4_optin" = 1 ] && [ "$tier4_base" != 1 ] && \
        printf '%s\n' "tier-4 base image REQUESTED (QDISTRO_BUILD_TIER4_BASE=1) but ABSENT: scenarios 56/57 will run and ERROR on the broken bake"
    [ "$legacy_ctrl" != 1 ] && \
        printf '%s\n' "legacy qdshell ctrl-socket absent (expected on the shipping Quickshell session): legacy qdwin/*.md scenarios skip by content"
    return 0
}

# Record the agent identity (H6a) into manifest.txt: the sanitized QCI_AGENT_CMD
# template, the model (QCI_AGENT_MODEL, parsed from `--model X`/`-m X`, or
# `unknown` when neither names one -- there is no default), and a
# best-effort agent CLI version. This is what distinguishes a CI run from a debug
# rerun with a stronger model, and is the prerequisite for never confusing debug
# rows with CI rows. Pure w.r.t. the run tree except the kv writes; a missing
# QCI_AGENT_CMD is a no-op. Model parsing is host-testable via
# gui_agent_model_from_cmd.
gui_agent_model_from_cmd() {
    local cmd=$1 model=""
    model=$(printf '%s' "$cmd" | grep -oE -- '(^|[[:space:]])(--model[= ]+|-m[= ]+)[A-Za-z0-9._:-]+' | head -1 \
        | sed -E 's/^[[:space:]]*//; s/^(--model|-m)[= ]+//')
    printf '%s' "$model"
}

record_agent_identity() {
    local cmd=${QCI_AGENT_CMD:-} model="" ver=""
    [ -n "$cmd" ] || return 0
    # Scrub tabs/newlines so the value stays a single manifest line.
    kv qci_agent_cmd "$(printf '%s' "$cmd" | tr '\t\n' '  ')"
    # Effective host-side agent work-timeout ceiling (seconds; 0 = unbounded). The
    # report flags attempts whose wall_s exceeds 90% of this so a too-tight ceiling
    # is tuned from data (§4 timeout near-miss). See run_agent_command's `to`.
    kv qci_agent_timeout_s "${QCI_AGENT_TIMEOUT:-0}"
    model=$(gui_agent_model_from_cmd "$cmd")
    [ -n "${QCI_AGENT_MODEL:-}" ] && model=$QCI_AGENT_MODEL
    # `unknown`, never a guessed default. QCI_AGENT_CMD is an arbitrary wrapper;
    # when neither --model/-m nor QCI_AGENT_MODEL names one, the effective model
    # is genuinely undetermined, and recording a guessed default made a debug
    # rerun with a different model indistinguishable from a CI row in exactly
    # the comparison this key exists to support.
    kv qci_agent_model "${model:-unknown}"
    # Best-effort CLI version — only if the template invokes a known agent binary,
    # and bounded so a wedged CLI cannot stall the gate.
    if printf '%s' "$cmd" | grep -qE '(^|[[:space:]/])claude([[:space:]]|$)'; then
        ver=$(timeout 10 claude --version 2>/dev/null | head -1)
        [ -n "$ver" ] && kv qci_agent_version "$ver"
    elif printf '%s' "$cmd" | grep -qE '(^|[[:space:]/])codex([[:space:]]|$)'; then
        ver=$(timeout 10 codex --version 2>/dev/null | head -1)
        [ -n "$ver" ] && kv qci_agent_version "$ver"
    fi
}

gate_gui() {
    qci_assert_run_dir || return $?
    # This must remain ahead of host-desktop setup, golden provisioning, and VM
    # acquisition. Invalid operator input is an args error, not expensive VM
    # infrastructure work and never a reason to start a visual agent.
    gui_validate_scenarios || return $?
    gui_isolate_host_desktop
    if ! command -v bwrap >/dev/null 2>&1; then
        record_blocked gui host-desktop-isolation "$EXIT_GUI" infra \
            "bubblewrap is required: refusing to expose GUI agents to the host desktop"
        return "$EXIT_GUI"
    fi
    kv gui_host_desktop_isolated 1
    kv gui_host_agent_sandbox bubblewrap
    qci_assert_vm_tools gui || return $?
    record_agent_identity
    local explicit=${1:-} svm qdwin_svm="" rc=$EXIT_OK scenario rel require step_rc legacy_ctrl=0 nested_kvm=0 qdshell_active=0
    require=${QCI_REQUIRE_AGENT_GUI:-1}
    # Build per-run GUI goldens once per profile. The admin profile keeps the
    # compositor-independent approval/broker scenarios available; the qdwin
    # profile runs qdwin/qdshell/qdshell-vision rows instead of pre-skipping them
    # just because the admin probe VM is not a qdwin session.
    if [ -z "$explicit" ] && [ "${QCI_NO_GOLDEN:-0}" != 1 ]; then
        ensure_run_golden gui-admin || return "$EXIT_VM_PROVISION"
        if [ "${QCI_GUI_SKIP_QDWIN:-0}" != 1 ]; then
            ensure_run_golden gui-qdwin || return "$EXIT_VM_PROVISION"
        fi
    fi
    # A single admin session VM is used for compositor-independent capability
    # probes. qdwin-specific sub-gates get a qdwin-profile session VM below.
    svm=$(acquire_vm gui-admin "$explicit") || return "$EXIT_VM_PROVISION"
    kv vm "$svm"
    if "$VM_TOOLS/vm-exec" "$svm" "runuser -u admin -- sh -c 'echo list | socat -t 2 - UNIX-CONNECT:/run/user/1000/qdshell.sock 2>/dev/null | head -1 | grep -qx \"ok list\"'" >/dev/null 2>&1; then
        legacy_ctrl=1
    fi
    if "$VM_TOOLS/vm-exec" "$svm" "test -e /dev/kvm" >/dev/null 2>&1; then
        nested_kvm=1
    fi
    if "$VM_TOOLS/vm-exec" "$svm" "test -S /run/user/1000/wayland-1 && runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user is-active qdshell.service >/dev/null 2>&1" >/dev/null 2>&1; then
        qdshell_active=1
    fi
    # Tier-4/5 opt-in base-image presence: absent + not-opted-in => clean SKIP
    # (the bake is opt-in, not a broken provision). See gui_scenario_skip_reason.
    local tier5_base=0 tier4_base=0
    "$VM_TOOLS/vm-exec" "$svm" "test -f /var/lib/libvirt/images/qdistro-tier5-base.qcow2" >/dev/null 2>&1 && tier5_base=1
    "$VM_TOOLS/vm-exec" "$svm" "test -f /var/lib/libvirt/images/qdistro-tier4-guest.qcow2" >/dev/null 2>&1 && tier4_base=1
    local tier5_optin=0 tier4_optin=0
    [ "${QDISTRO_BUILD_TIER5_BASE:-0}" = 1 ] && tier5_optin=1
    [ "${QDISTRO_BUILD_TIER4_BASE:-0}" = 1 ] && tier4_optin=1

    # Preflight capability summary (reporting only): surface absent SHARED
    # preconditions ONCE before the scenario pool spends VMs/agents, rather than
    # leaving the reader to infer a whole-lane-down profile gap from N skip rows.
    # Reuses the same capability flags; does not change any dispatch decision.
    local preflight_log="$RDIR/gui/preflight.txt" preflight_obs
    preflight_obs=$(gui_preflight_capabilities "${QCI_GUI_SKIP_QDWIN:-0}" \
        "$qdshell_active" "$nested_kvm" "$legacy_ctrl" "${VM_SSH_PORT:-}" \
        "$tier5_base" "$tier4_base" "$tier5_optin" "$tier4_optin")
    # Text-corroboration probe for the visual-evidence contract. Unlike the
    # flags above this is a HOST capability (the gate OCRs the attested frames
    # it harvested), and like them it is REPORTING-ONLY: an absent backend
    # degrades the per-frame text column to `skip` and changes no verdict.
    # Grading depends on the sealed VM-bound ledger and a vision-capable runner,
    # neither of which OCR can supply. Surfaced here so a run whose text column
    # is empty is explained up front rather than looking like a defect.
    local ocr_backend visual_obs
    ocr_backend=$(gui_ocr_backend_probe) || ocr_backend=""
    kv gui_visual_evidence_backend "${ocr_backend:-none}"
    if [ -z "$ocr_backend" ]; then
        # Only an ABSENT backend is a gap worth an observation row; a present one
        # is recorded on the log's header line and in the manifest.
        visual_obs=$(gui_visual_backend_observation "$ocr_backend")
        preflight_obs=$(printf '%s\n%s\n' "$preflight_obs" "$visual_obs" | grep -v '^[[:space:]]*$' || true)
    fi
    {
        echo "# GUI preflight capability summary"
        echo "session VM: $svm"
        echo "visual-evidence backend: ${ocr_backend:-NONE}"
        echo "flags: skip_qdwin=${QCI_GUI_SKIP_QDWIN:-0} qdshell_active=$qdshell_active nested_kvm=$nested_kvm legacy_ctrl=$legacy_ctrl vm_ssh_port=${VM_SSH_PORT:-} tier5_base=$tier5_base tier4_base=$tier4_base tier5_optin=$tier5_optin tier4_optin=$tier4_optin"
        echo
        if [ -n "$preflight_obs" ]; then
            echo "Observations (lanes that will skip / run-and-error this run):"
            printf '%s\n' "$preflight_obs" | sed 's/^/- /'
        else
            echo "Fully capable profile: no shared precondition is absent."
        fi
    } > "$preflight_log"
    if [ -n "$preflight_obs" ]; then
        local obs_count
        obs_count=$(printf '%s\n' "$preflight_obs" | grep -c .)
        log "gui preflight: $obs_count shared-capability observation(s) — see gui/preflight.txt"
        record_result gui preflight skip 0 pass agent "$preflight_log" \
            "$obs_count shared-capability observation(s); some lanes will skip (see log)"
    else
        record_result gui preflight pass 0 pass agent "$preflight_log" \
            "fully capable GUI profile; no shared precondition absent"
    fi

    if [ "${QCI_GUI_SKIP_QDWIN:-0}" = 1 ] || [ -n "$explicit" ]; then
        qdwin_svm=$svm
    else
        qdwin_svm=$(acquire_vm gui-qdwin "") || {
            collect_vm_artifacts "$svm" gui
            release_vm "$svm" "$EXIT_VM_PROVISION"
            return "$EXIT_VM_PROVISION"
        }
        kv vm_qdwin "$qdwin_svm"
    fi

    # App-deps capability: the qdwin app-compatibility scenarios need real desktop
    # apps that only exist when the golden was built with QDWIN_APP_DEPS=1. Probe
    # the qdwin session VM for the canonical app-dep (`foot`) — authoritative for
    # what the cloned qdwin workers will have, regardless of whether a golden was
    # built. When absent, qdwin/tests/apps/* SKIP deterministically before the
    # agent starts (see gui_scenario_app_deps_skip_reason).
    local app_deps=0
    if "$VM_TOOLS/vm-exec" "$qdwin_svm" "command -v foot >/dev/null 2>&1" >/dev/null 2>&1; then
        app_deps=1
    fi
    kv gui_app_deps "$app_deps"
    log "gui: qdwin app-deps capability app_deps=$app_deps (QDWIN_APP_DEPS golden knob; 0 => qdwin/tests/apps/* skip)"

    # Shell-capture capability probe (once, from the service MainPID environ).
    # Every qdwin visual assertion now flows through the in-compositor
    # shell-authorized capture; a golden baked before QDWIN_ENABLE_SHELL_CAPTURE
    # cannot produce visual evidence, so ALL capture-dependent lanes (smoke,
    # qdshell-ui vision, qdwin markdown scenarios) skip CONSISTENTLY with the
    # same rebake hint instead of hard-failing one by one.
    local qdwin_capture=1 capture_env
    if [ "${QCI_GUI_SKIP_QDWIN:-0}" != 1 ] && [ -n "$qdwin_svm" ]; then
        capture_env=$("$VM_TOOLS/vm-exec" "$qdwin_svm" "pid=\$(runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user show qdwin-compositor.service -p MainPID --value); tr '\0' '\n' </proc/\$pid/environ 2>/dev/null | grep -c '^QDWIN_ENABLE_SHELL_CAPTURE=1\$'" 2>/dev/null | grep -v '^\[vm-exec\]' | tr -d '\r')
        [ "${capture_env:-0}" = "1" ] || qdwin_capture=0
    fi
    kv gui_qdwin_capture "$qdwin_capture"
    log "gui: qdwin shell-capture capability qdwin_capture=$qdwin_capture (0 => golden predates QDWIN_ENABLE_SHELL_CAPTURE; rebake with fresh-vm-bootstrap; capture-dependent lanes skip)"

    run_qdwin_executable_gui_smokes "$qdwin_svm" "$qdwin_capture"; step_rc=$?
    [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc
    # The vision harness needs a LIVE qdshell quickshell session on wayland-1.
    # Authoritatively probe qdshell.service (the deployed qs unit) + the
    # wayland-1 socket rather than relying solely on the earlier
    # qdshell_active flag, which matches the broader scenario gating.
    local qdshell_session=0
    if [ "${QCI_GUI_SKIP_QDWIN:-0}" != 1 ] && { [ "$qdshell_active" = 1 ] || "$VM_TOOLS/vm-exec" "$qdwin_svm" "test -S /run/user/1000/wayland-1 && runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user is-active qdshell.service 2>/dev/null | grep -qx active" >/dev/null 2>&1; }; then
        qdshell_session=1
    fi
    if [ "${QCI_GUI_SKIP_QDWIN:-0}" = 1 ]; then
        record_result gui qdshell-ui skip 0 pass vision "" \
            "QCI_GUI_SKIP_QDWIN=1: qdwin/qdshell vision harness skipped"
    elif [ "$qdwin_capture" = 0 ]; then
        record_result gui qdshell-ui skip 0 pass vision "" \
            "golden lacks QDWIN_ENABLE_SHELL_CAPTURE=1 (vision harness needs the shell-capture path; rebake with fresh-vm-bootstrap)"
    elif [ "$qdshell_session" = 1 ]; then
        gate_qdshell_ui_agent "$qdwin_svm"; step_rc=$?
        [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc
    else
        record_result gui qdshell-ui skip 0 pass vision "" \
            "qdshell/noctalia session not active on wayland-1 in this VM profile; vision harness needs a live qdshell session"
    fi

    # The session VM has done its job (capability probe + per-session sub-gates).
    # When NO explicit VM was given, free it before the per-scenario pool so its
    # RAM is available to workers. With an explicit --vm, keep it: scenarios run
    # serially ON that VM (preserving the old `qci gui --vm` contract — the probed
    # VM and the scenario VM must be the same one).
    if [ -z "$explicit" ]; then
        collect_vm_artifacts "$svm" gui
        release_vm "$svm" "$rc"
        if [ -n "$qdwin_svm" ] && [ "$qdwin_svm" != "$svm" ]; then
            collect_vm_artifacts "$qdwin_svm" gui-qdwin
            release_vm "$qdwin_svm" "$rc"
        fi
    fi

    # Partition scenarios: admin-profile scenarios are gated by the admin probe
    # VM, while qdwin-dependent scenarios are routed to qdwin-profile workers
    # unless qdwin was explicitly disabled for an admin-only run.
    local to_run=() log_path
    while IFS= read -r scenario; do
        rel=$(gui_scenario_rel "$scenario")
        log_path="$RDIR/gui/$(safe_name "$rel").agent.log"
        mkdir -p "$(dirname "$log_path")"
        # QCI_OFFLINE annotation hook: registry network=external GUI scenarios
        # self-skip in offline mode. The registry key is qdistro-repo-relative.
        if [ "$QCI_OFFLINE" = 1 ] && offline_should_skip_external "${scenario#$QDISTRO_REPO/}"; then
            record_result gui "$rel" skip 0 pass agent "" "QCI_OFFLINE=1: registry network=external; skipped"
            continue
        fi
        # Stack-absent SKIP gate (see gui_scenario_skip_reason). When the VM
        # profile lacks the OUTER stack a scenario needs (legacy ctrl-socket,
        # qdshell/wayland-1 session, nested KVM, SSH transport), short-circuit to
        # SKIP up front — no VM spent, no agent dispatched. For tier-4/5: an
        # absent OPT-IN base image is SKIPped here when the run did not opt in
        # (gui_scenario_tier_base_skip_reason); but if the run DID opt in
        # (QDISTRO_BUILD_TIER{4,5}_BASE=1) yet the bake is still missing/broken,
        # the scenario reaches the agent, which reports ERROR per the scenarios'
        # own "do not silently skip a requested bake" contract.
        local skip_reason
        # Legacy ctrl-socket scenarios (removed qdshell.py API) can never pass
        # against the shipping Quickshell session — skip them deterministically by
        # content, in EVERY path (this runs before the qdwin-routing bypass below
        # so routing qdwin scenarios to the qdwin profile doesn't unleash them as
        # agent ERRORs). Opt into a legacy lane with QCI_GUI_RUN_LEGACY_QDWIN_MD=1.
        local tier_base_skip app_deps_skip xwayland_skip
        tier_base_skip=$(gui_scenario_tier_base_skip_reason "$rel" \
            "$tier5_base" "$tier4_base" "$tier5_optin" "$tier4_optin")
        app_deps_skip=$(gui_scenario_app_deps_skip_reason "$rel" "$app_deps")
        xwayland_skip=$(gui_scenario_xwayland_skip_reason "$rel" "${QCI_XWAYLAND_E2E:-0}")
        if [ "${QCI_GUI_RUN_LEGACY_QDWIN_MD:-0}" != 1 ] && gui_scenario_uses_legacy_ctrl "$scenario"; then
            skip_reason="legacy qdshell.py ctrl-socket scenario not supported by the Quickshell qdshell session"
        elif [ -n "$xwayland_skip" ]; then
            skip_reason="$xwayland_skip"
        elif [ -n "$tier_base_skip" ]; then
            # Opt-in tier-4/5 base image absent (and not opted in): clean SKIP.
            # Runs BEFORE the qdwin-routing bypass below so it actually fires for
            # these qdwin-required scenarios in the default lane.
            skip_reason="$tier_base_skip"
        elif [ -n "$app_deps_skip" ]; then
            # qdwin app-compatibility scenario against a golden with no app deps:
            # deterministic SKIP naming the missing capability, before the agent
            # starts. Runs BEFORE the qdwin-routing bypass (app scenarios are
            # qdwin-required) so it actually fires in the default lean lane.
            skip_reason="$app_deps_skip"
        elif [ "${QCI_GUI_SKIP_QDWIN:-0}" != 1 ] && [ "$qdwin_capture" = 0 ] && \
                gui_scenario_requires_qdwin "$rel"; then
            # Golden predates the in-compositor shell-capture path: every
            # visual assertion in a qdwin scenario would fail at the first
            # qdwin_screenshot. Same capability skip as the smoke/vision
            # lanes — one consistent rebake signal, not N confusing failures.
            skip_reason="golden lacks QDWIN_ENABLE_SHELL_CAPTURE=1 (qdwin_screenshot needs the shell-capture path); rebake the golden with fresh-vm-bootstrap"
        elif [ -z "$explicit" ] && [ "${QCI_GUI_SKIP_QDWIN:-0}" != 1 ] && gui_scenario_requires_qdwin "$rel"; then
            skip_reason=""
        else
            skip_reason=$(gui_scenario_skip_reason "$rel" "$legacy_ctrl" "$nested_kvm" \
                "$qdshell_active" "${VM_SSH_PORT:-}" "${QCI_GUI_SKIP_QDWIN:-0}")
        fi
        if [ -n "$skip_reason" ]; then
            {
                echo "Skipped GUI scenario."
                echo "Scenario: $scenario"
                echo "Reason: $skip_reason"
            } > "$log_path"
            record_result gui "$rel" skip 0 pass agent "$log_path" "$skip_reason"
            continue
        fi
        if [ -z "${QCI_AGENT_CMD:-}" ]; then
            {
                echo "QCI_AGENT_CMD is not set."
                echo "Scenario: $scenario"
            } > "$log_path"
            if [ "$require" = 1 ]; then
                record_blocked gui "$rel" "$EXIT_GUI" agent "agent runner not configured" "$log_path"
                [ "$rc" -eq 0 ] && rc=$EXIT_GUI
            else
                record_result gui "$rel" skip 0 pass agent "$log_path" "agent runner not configured"
            fi
            continue
        fi
        to_run+=("$scenario")
    done < <(agent_scenarios)

    if [ "${#to_run[@]}" -gt 0 ]; then
        local frc
        if [ -n "$explicit" ]; then
            # Explicit --vm: run every scenario serially ON that VM (single-tenant
            # GUI session), then release it once at the end.
            for scenario in "${to_run[@]}"; do
                gui_run_scenario "$scenario" "$svm"
                frc=$?
                [ "$frc" -ne 0 ] && [ "$rc" -eq 0 ] && rc=$frc
            done
            collect_vm_artifacts "$svm" gui
            release_vm "$svm" "$rc"
        else
            # Disposable: parallel pool, one fresh GUI VM per scenario.
            local jobs running=0 frag=0 worker_id
            jobs=$(gui_job_count)
            # Concurrency visibility (Phase 0 / H8): record requested vs effective
            # (RAM-clamped) GUI parallelism in manifest.txt.
            kv gui_jobs_requested "${QCI_GUI_JOBS:-1}"
            kv gui_jobs_effective "$jobs"
            # Route each concurrent worker's result rows to a per-worker fragment
            # (merged at finish_run) so parallel appends never interleave and a
            # crashed worker's rows survive. Auto-on when >1 worker; operator forces
            # off with QCI_RESULT_FRAGMENTS=0.
            [ "$jobs" -gt 1 ] && frag=${QCI_RESULT_FRAGMENTS:-1}
            log "gui gate: ${#to_run[@]} agent scenarios on disposable VMs, up to $jobs in parallel (set QCI_GUI_JOBS to override)"
            for scenario in "${to_run[@]}"; do
                worker_id=$(worker_fragment_id gui "${scenario#"$WORKSPACE"/}")
                QCI_RESULT_FRAGMENTS="$frag" QCI_WORKER_ID="$worker_id" \
                    gui_run_scenario "$scenario" &
                running=$((running + 1))
                if [ "$running" -ge "$jobs" ]; then
                    wait -n; frc=$?
                    [ "$frc" -ne 0 ] && [ "$rc" -eq 0 ] && rc=$frc
                    running=$((running - 1))
                fi
            done
            while [ "$running" -gt 0 ]; do
                wait -n; frc=$?
                [ "$frc" -ne 0 ] && [ "$rc" -eq 0 ] && rc=$frc
                running=$((running - 1))
            done
        fi
    elif [ -n "$explicit" ]; then
        # No runnable scenarios but we kept the explicit session VM — release it.
        collect_vm_artifacts "$svm" gui
        release_vm "$svm" "$rc"
    fi
    return "$rc"
}
