#!/usr/bin/env bash
# qci module: gui gate + agent scenarios
# Extracted verbatim from bin/qci. SOURCED by bin/qci into the single
# CI-runner process (shared RDIR/CREATED_VMS/golden state/traps); it is
# NOT executed standalone. See ci/AGENTS.md for the module map.
# shellcheck shell=bash

run_qdwin_executable_gui_smokes() {
    local vm=$1 rc=$EXIT_OK scenario file step_rc
    export VMNAME="$vm"
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
    loaded_lw=$("$VM_TOOLS/vm-exec" "$vm" "pmap \$(pgrep -x weston | head -n1) 2>/dev/null | grep -o '/[^ ]*libweston-14\.so[^ ]*' | sort -u | head -n1" 2>/dev/null | grep -v '^\[vm-exec\]' | tr -d '\r')
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
    return "$rc"
}

gui_scenario_requires_qdwin() {
    local rel=$1
    case "$rel" in
        qdwin/tests/gui/[0-9][0-9]-*.md|\
        qdwin/tests/apps/[0-9][0-9]-*.md|\
        qdistro/tests/integration/qdwin-noctalia/[0-9][0-9]-*.md|\
        qdlocker/tests/gui/[0-9][0-9]-*.md|\
        qdistro/tests/integration/permissions-gui/18-podapps-launcher-badge.md|\
        qdistro/tests/integration/permissions-gui/19-tier5-loopback-visible.md|\
        qdistro/tests/integration/permissions-gui/20-tier5-vm-cold-start.md|\
        qdistro/tests/integration/permissions-gui/21-tier5-close-cleanup.md|\
        qdistro/tests/integration/permissions-gui/56-tier4-rdp-window-visible.md|\
        qdistro/tests/integration/permissions-gui/57-tier4-rdp-close-cleanup.md)
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

agent_scenarios() {
    local f
    for f in \
        "$WORKSPACE"/qdwin/tests/gui/[0-9][0-9]-*.md \
        "$WORKSPACE"/qdwin/tests/apps/[0-9][0-9]-*.md \
        "$WORKSPACE"/qdistro/tests/integration/permissions-gui/[0-9][0-9]-*.md \
        "$WORKSPACE"/qdistro/tests/integration/qdwin-noctalia/[0-9][0-9]-*.md \
        "$WORKSPACE"/qdlocker/tests/gui/[0-9][0-9]-*.md
    do
        [ -f "$f" ] && printf '%s\n' "$f"
    done
}

write_agent_prompt() {
    local vm=$1 scenario=$2 prompt=$3 rel
    rel=${scenario#$WORKSPACE/}
    cat > "$prompt" <<EOF
# qdistro CI GUI scenario runner

Run this scenario against VM \`$vm\` and write a PASS/FAIL/ERROR report.

Scenario file:
\`$scenario\`

Artifact directory:
\`$RDIR\`

Rules:
- Read the nearest AGENTS.md before executing the scenario.
- Do not edit source files.
- Save screenshots, OCR output, command logs, and notes under:
  \`$RDIR/gui/$(safe_name "$rel")/\`
- Before returning, write \`$RDIR/gui/$(safe_name "$rel")/status.txt\`
  containing exactly one word: PASS, FAIL, ERROR, or SKIP.
- Use VMNAME=$vm.
- Execute setup, steps, assertions, and cleanup serially.
- Return nonzero on FAIL or ERROR. Return 0 only when every required assertion passes.
- If the failure is a missing qdwin/Wayland protocol, point at qdwin or libweston integration, not a qdshell workaround.

Start by reading:
- \`$scenario\`
- \`$(dirname "$scenario")/AGENTS.md\` if present, otherwise the closest parent AGENTS.md.
EOF
}

agent_artifact_status() {
    local artifact_dir=$1 log_path=$2 raw=""
    if [ -f "$artifact_dir/status.txt" ]; then
        raw=$(tr -d '\r' < "$artifact_dir/status.txt" | awk 'NF {print toupper($1); exit}')
    elif [ -f "$artifact_dir/report.md" ]; then
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

# Host-side waiter: retry a guest command over vm-exec until it exits 0 or the
# deadline passes — the host equivalent of the guest await_* helpers, for
# host-driven readiness gates. Bounded by the wall clock; on timeout logs the
# last guest output. Returns 0 on success, 1 on timeout. VM_TOOLS is overridable
# so this is host-testable with a fake vm-exec.
# Args: vm timeout_s interval_s <guest-cmd...>
await_vmexec_success() {
    local vm=$1 timeout=$2 interval=$3; shift 3
    local start=$SECONDS last elapsed
    while :; do
        if last=$("$VM_TOOLS/vm-exec" "$vm" "$*" 2>&1); then
            return 0
        fi
        elapsed=$((SECONDS - start))
        if [ "$elapsed" -ge "$timeout" ]; then
            log "await_vmexec_success: TIMEOUT ${elapsed}s on '$*' (last: ${last:0:200})"
            return 1
        fi
        sleep "$interval"
    done
}

# Pure status/rc -> verdict mapper for one agent scenario attempt. FAIL CLOSED:
# a pass is recorded ONLY for an explicit PASS with rc=0. SKIP passes through as
# skip. Everything else — FAIL/ERROR, UNKNOWN (agent exited without a parseable
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
        SKIP:*)
            printf 'skip\tagent scenario skipped' ;;
        FAIL:*|ERROR:*)
            printf 'fail\tagent status=%s rc=%s' "$status" "$rc" ;;
        *)
            printf 'fail\tagent command rc=%s status=%s (no usable verdict — fail closed)' "$rc" "$status" ;;
    esac
}

run_agent_command() {
    local prompt=$1 log_path=$2 cmd=${QCI_AGENT_CMD:-} expanded
    if [ -z "$cmd" ]; then
        return 127
    fi
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
    if [[ "$cmd" == *"{prompt}"* ]]; then
        expanded=${cmd//\{prompt\}/$prompt}
        if [ "$to" -gt 0 ]; then
            timeout -k 15 "$to" bash -lc "$expanded" < /dev/null > "$log_path" 2>&1
        else
            bash -lc "$expanded" < /dev/null > "$log_path" 2>&1
        fi
    else
        if [ "$to" -gt 0 ]; then
            # shellcheck disable=SC2086
            timeout -k 15 "$to" $cmd "$prompt" < /dev/null > "$log_path" 2>&1
        else
            # shellcheck disable=SC2086
            $cmd "$prompt" < /dev/null > "$log_path" 2>&1
        fi
    fi
}

gate_qdshell_ui_agent() {
    # Runs the qdshell agent-assisted UI vision pytest. The harness drives the
    # LIVE qdshell session inside the qdwin VM acquired by gate_gui (IPC over
    # wayland-1 via vm-exec, screenshots via `virsh screenshot`), because the
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
    local scenario=$1 provided=${2:-} rel vm prompt log_path status agent_rc frc=0 own=0 t0 t1 t2 ta0 ta1 gate_name
    rel=${scenario#$WORKSPACE/}
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
    prompt="$RDIR/agent-notes/$(safe_name "$rel").prompt.md"
    log_path="$RDIR/gui/$(safe_name "$rel").agent.log"
    mkdir -p "$(dirname "$log_path")"
    write_agent_prompt "$vm" "$scenario" "$prompt"
    # Deliver the guest waiter library so the scenario can source
    # /tmp/qci-gui-waiters.sh (best-effort; a scenario that needs it and lacks it
    # fails its own assertion loudly).
    install_gui_waiters "$vm" || log "agent scenario $rel: waiter-lib delivery failed (continuing)"
    log "agent scenario $rel on $vm"
    record_host_load gui "$rel" start
    ta0=$(date +%s)
    run_agent_command "$prompt" "$log_path"
    agent_rc=$?
    ta1=$(date +%s)
    record_host_load gui "$rel" end
    status=$(agent_artifact_status "$RDIR/gui/$(safe_name "$rel")" "$log_path")
    # Per-attempt observability row: the RAW agent status + rc + wall seconds,
    # before the fail-closed verdict mapping collapses it to pass/fail/skip. This
    # is where the flake signal lives (rc=124, UNKNOWN, slow walls under load).
    record_attempt gui "$rel" 1 "$status" "$agent_rc" "" "$((ta1 - ta0))" "$vm" "$log_path"
    # Fail-closed status/rc mapping (see gui_agent_verdict). UNKNOWN:0 — an agent
    # that exited 0 without rendering a usable verdict — is a hard failure here,
    # not the silent pass it used to be.
    local verdict note
    IFS=$'\t' read -r verdict note < <(gui_agent_verdict "$status" "$agent_rc")
    case "$verdict" in
        pass) record_result gui "$rel" pass 0 pass agent "$log_path" "$note" ;;
        skip) record_result gui "$rel" skip 0 pass agent "$log_path" "$note" ;;
        *)    record_result gui "$rel" fail "$EXIT_GUI" gui agent "$log_path" "$note"
              frc=$EXIT_GUI ;;
    esac
    t2=$(date +%s)
    collect_vm_artifacts "$vm" "gui-$(safe_name "$rel")"
    [ "$own" = 1 ] && release_vm "$vm" "$frc"
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
        qdwin/tests/gui/[0-9][0-9]-*.md|qdwin/tests/apps/[0-9][0-9]-*.md)
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

gate_gui() {
    qci_assert_run_dir || return $?
    qci_assert_vm_tools gui || return $?
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
    {
        echo "# GUI preflight capability summary"
        echo "session VM: $svm"
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

    run_qdwin_executable_gui_smokes "$qdwin_svm"; step_rc=$?
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
        rel=${scenario#$WORKSPACE/}
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
        local tier_base_skip
        tier_base_skip=$(gui_scenario_tier_base_skip_reason "$rel" \
            "$tier5_base" "$tier4_base" "$tier5_optin" "$tier4_optin")
        if [ "${QCI_GUI_RUN_LEGACY_QDWIN_MD:-0}" != 1 ] && gui_scenario_uses_legacy_ctrl "$scenario"; then
            skip_reason="legacy qdshell.py ctrl-socket scenario not supported by the Quickshell qdshell session"
        elif [ -n "$tier_base_skip" ]; then
            # Opt-in tier-4/5 base image absent (and not opted in): clean SKIP.
            # Runs BEFORE the qdwin-routing bypass below so it actually fires for
            # these qdwin-required scenarios in the default lane.
            skip_reason="$tier_base_skip"
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
            local jobs running=0
            jobs=$(gui_job_count)
            log "gui gate: ${#to_run[@]} agent scenarios on disposable VMs, up to $jobs in parallel (set QCI_GUI_JOBS to override)"
            for scenario in "${to_run[@]}"; do
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
