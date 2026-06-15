#!/usr/bin/env bash
# qci module: gui gate + agent scenarios
# Extracted verbatim from bin/qci. SOURCED by bin/qci into the single
# CI-runner process (shared RDIR/CREATED_VMS/golden state/traps); it is
# NOT executed standalone. See ci/AGENTS.md for the module map.
# shellcheck shell=bash

run_qdwin_executable_gui_smokes() {
    local vm=$1 rc=$EXIT_OK scenario file step_rc
    export VMNAME="$vm"
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

run_agent_command() {
    local prompt=$1 log_path=$2 cmd=${QCI_AGENT_CMD:-} expanded
    if [ -z "$cmd" ]; then
        return 127
    fi
    if [[ "$cmd" == *"{prompt}"* ]]; then
        expanded=${cmd//\{prompt\}/$prompt}
        bash -lc "$expanded" < /dev/null > "$log_path" 2>&1
    else
        # shellcheck disable=SC2086
        $cmd "$prompt" < /dev/null > "$log_path" 2>&1
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
# process, so this is capped separately from bats. Default 8; QCI_GUI_JOBS
# overrides. RAM-clamped like the bats pool.
gui_job_count() {
    local jobs ram_gb ram_cap
    if [ -n "${QCI_GUI_JOBS:-}" ] && [ "${QCI_GUI_JOBS}" -ge 1 ] 2>/dev/null; then
        jobs=$QCI_GUI_JOBS
    else
        jobs=8
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
    local scenario=$1 provided=${2:-} rel vm prompt log_path status agent_rc frc=0 own=0 t0 t1 t2
    rel=${scenario#$WORKSPACE/}
    t0=$(date +%s)
    if [ -n "$provided" ]; then
        vm=$provided
    else
        vm=$(acquire_vm "gui-$(safe_name "$rel")" "") || {
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
    log "agent scenario $rel on $vm"
    run_agent_command "$prompt" "$log_path"
    agent_rc=$?
    status=$(agent_artifact_status "$RDIR/gui/$(safe_name "$rel")" "$log_path")
    case "$status:$agent_rc" in
        PASS:0|UNKNOWN:0)
            record_result gui "$rel" pass 0 pass agent "$log_path" "agent scenario passed" ;;
        SKIP:*)
            record_result gui "$rel" skip 0 pass agent "$log_path" "agent scenario skipped" ;;
        FAIL:*|ERROR:*)
            record_result gui "$rel" fail "$EXIT_GUI" gui agent "$log_path" "agent status=$status rc=$agent_rc"
            frc=$EXIT_GUI ;;
        *)
            record_result gui "$rel" fail "$EXIT_GUI" gui agent "$log_path" "agent command rc=$agent_rc status=$status"
            frc=$EXIT_GUI ;;
    esac
    t2=$(date +%s)
    collect_vm_artifacts "$vm" "gui-$(safe_name "$rel")"
    [ "$own" = 1 ] && release_vm "$vm" "$frc"
    record_timing gui "$rel" "$((t1 - t0))" "$((t2 - t1))" "$((t2 - t0))" "$frc" "$vm"
    return "$frc"
}

gate_gui() {
    qci_assert_run_dir || return $?
    qci_assert_vm_tools gui || return $?
    local explicit=${1:-} svm rc=$EXIT_OK scenario rel require step_rc legacy_ctrl=0 nested_kvm=0 noctalia_shell=0
    require=${QCI_REQUIRE_AGENT_GUI:-1}
    # A single "session" VM is used for the capability probe + the per-session
    # sub-gates (executable smokes, qdshell-ui vision). The agent SCENARIOS then
    # each run on their own disposable VM in a parallel pool below.
    svm=$(acquire_vm gui "$explicit") || return "$EXIT_VM_PROVISION"
    kv vm "$svm"
    if "$VM_TOOLS/vm-exec" "$svm" "runuser -u admin -- sh -c 'echo list | socat -t 2 - UNIX-CONNECT:/run/user/1000/qdshell.sock 2>/dev/null | head -1 | grep -qx \"ok list\"'" >/dev/null 2>&1; then
        legacy_ctrl=1
    fi
    if "$VM_TOOLS/vm-exec" "$svm" "test -e /dev/kvm" >/dev/null 2>&1; then
        nested_kvm=1
    fi
    if "$VM_TOOLS/vm-exec" "$svm" "test -S /run/user/1000/wayland-1 && pgrep -a noctalia-shell >/dev/null" >/dev/null 2>&1; then
        noctalia_shell=1
    fi

    run_qdwin_executable_gui_smokes "$svm"; step_rc=$?
    [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc
    # The vision harness needs a LIVE qdshell quickshell session on wayland-1.
    # Authoritatively probe noctalia-shell.service (the deployed qs unit) +
    # the wayland-1 socket rather than relying solely on the pgrep-based
    # noctalia_shell flag, which matches the broader scenario gating.
    local qdshell_session=0
    if [ "$noctalia_shell" = 1 ] || "$VM_TOOLS/vm-exec" "$svm" "test -S /run/user/1000/wayland-1 && runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user is-active noctalia-shell.service qdshell.service 2>/dev/null | grep -qx active" >/dev/null 2>&1; then
        qdshell_session=1
    fi
    if [ "$qdshell_session" = 1 ]; then
        gate_qdshell_ui_agent "$svm"; step_rc=$?
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
    fi

    # Partition scenarios: those gated out by this VM profile are recorded as
    # skips up front (no VM spent). The capability flags are identical across
    # GUI VMs (same spin profile), so the probe on the session VM applies to
    # every worker VM. The rest run one-VM-each in a parallel pool.
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
        case "$rel" in
            qdwin/tests/gui/[0-9][0-9]-*.md|qdwin/tests/apps/[0-9][0-9]-*.md)
                if [ "$legacy_ctrl" != 1 ]; then
                    {
                        echo "Skipped legacy qdwin markdown scenario."
                        echo "Scenario: $scenario"
                        echo "Reason: /run/user/1000/qdshell.sock is absent; current qdistro GUI session is Quickshell/noctalia based."
                    } > "$log_path"
                    record_result gui "$rel" skip 0 pass agent "$log_path" "legacy qdshell ctrl-socket not available"
                    continue
                fi
                ;;
            qdistro/tests/integration/permissions-gui/20-tier5-vm-cold-start.md|\
            qdistro/tests/integration/permissions-gui/21-tier5-close-cleanup.md|\
            qdistro/tests/integration/permissions-gui/56-tier4-rdp-window-visible.md|\
            qdistro/tests/integration/permissions-gui/57-tier4-rdp-close-cleanup.md)
                if [ "$nested_kvm" != 1 ]; then
                    {
                        echo "Skipped nested-VM GUI scenario."
                        echo "Scenario: $scenario"
                        echo "Reason: /dev/kvm is absent inside the session VM; this scenario requires nested KVM."
                    } > "$log_path"
                    record_result gui "$rel" skip 0 pass agent "$log_path" "nested KVM not available in VM"
                    continue
                fi
                ;;
            qdistro/tests/integration/permissions-gui/18-podapps-launcher-badge.md|\
            qdistro/tests/integration/permissions-gui/19-tier5-loopback-visible.md)
                if [ "$noctalia_shell" != 1 ]; then
                    {
                        echo "Skipped qdshell/noctalia GUI scenario."
                        echo "Scenario: $scenario"
                        echo "Reason: /run/user/1000/wayland-1 or noctalia-shell is absent; this VM profile runs labwc for permissions-gui admin app scenarios."
                    } > "$log_path"
                    record_result gui "$rel" skip 0 pass agent "$log_path" "noctalia/qdshell session not active in this VM profile"
                    continue
                fi
                ;;
            qdistro/tests/integration/qdwin-noctalia/[0-9][0-9]-*.md)
                if [ "$noctalia_shell" != 1 ]; then
                    {
                        echo "Skipped qdwin/noctalia GUI scenario."
                        echo "Scenario: $scenario"
                        echo "Reason: /run/user/1000/wayland-1 or noctalia-shell is absent; this VM profile runs labwc for permissions-gui admin app scenarios."
                    } > "$log_path"
                    record_result gui "$rel" skip 0 pass agent "$log_path" "noctalia/qdshell session not active in this VM profile"
                    continue
                fi
                ;;
            qdlocker/tests/gui/[0-9][0-9]-*.md)
                if [ "$noctalia_shell" != 1 ]; then
                    {
                        echo "Skipped qdlocker GUI scenario."
                        echo "Scenario: $scenario"
                        echo "Reason: /run/user/1000/wayland-1 or noctalia-shell is absent; qdlocker GUI tests require the qdwin/noctalia session, while this VM profile runs labwc for permissions-gui admin app scenarios."
                    } > "$log_path"
                    record_result gui "$rel" skip 0 pass agent "$log_path" "noctalia/qdshell session not active in this VM profile"
                    continue
                fi
                ;;
            qdistro/tests/integration/permissions-gui/55-qsu-selinux-enforcing.md)
                if [ -z "${VM_SSH_PORT:-}" ]; then
                    {
                        echo "Skipped SSH-only SELinux GUI scenario."
                        echo "Scenario: $scenario"
                        echo "Reason: VM_SSH_PORT is not set; this scenario explicitly requires SSH transport because qemu-guest-agent cannot set SELinux enforcing mode."
                    } > "$log_path"
                    record_result gui "$rel" skip 0 pass agent "$log_path" "VM_SSH_PORT not set for SSH-only SELinux scenario"
                    continue
                fi
                ;;
        esac
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
