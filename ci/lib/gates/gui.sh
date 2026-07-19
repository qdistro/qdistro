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

# Reject an explicit scenario typo before qci builds a golden, starts a VM, or
# launches a GUI-capable agent. The normal scenario producer emits only files
# found by globs, but `qci gui --scenario ...` replaces it with operator input;
# letting a missing path reach the worker used to spend a full image bake and
# then invite the model to improvise a different scenario.
gui_validate_scenarios() {
    local scenario rc=0
    while IFS= read -r scenario; do
        if [ -z "$scenario" ]; then
            record_blocked gui '<missing>' "$EXIT_USAGE" args \
                "--scenario requires an existing readable .md file"
            rc=$EXIT_USAGE
        elif [ "${scenario##*.}" != md ] || [ ! -f "$scenario" ] || [ ! -r "$scenario" ]; then
            record_blocked gui "$scenario" "$EXIT_USAGE" args \
                "GUI scenario must be an existing readable .md file; rejected before VM provisioning"
            rc=$EXIT_USAGE
        fi
    done < <(agent_scenarios)
    return "$rc"
}

write_agent_prompt() {
    local vm=$1 scenario=$2 prompt=$3 artifact_dir=${4:-} scratch=${5:-} slug=${6:-} rel
    rel=$(gui_scenario_rel "$scenario")
    # Per-attempt artifact dir so a retry's agent writes its status/report to its
    # OWN directory and never clobbers the first attempt's evidence (the audit
    # trail that makes classified retry acceptable). Defaults to the canonical dir.
    [ -n "$artifact_dir" ] || artifact_dir="$RDIR/gui/$(safe_name "$rel")"
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
- Every graphical process, dialog, compositor, and input action belongs inside
  the disposable VM named above. Never launch a host GUI program (including
  virt-manager, virt-viewer, remote-viewer, xdg-open, or an app under the host
  DISPLAY/Wayland session). Drive the guest only through the repository's
  vm-exec/vm-gui helpers and virsh. qci deliberately makes the host desktop
  sockets unavailable to this agent process.
- Save screenshots, OCR output, command logs, and notes under:
  \`$artifact_dir/\`
- Before returning, write \`$artifact_dir/status.txt\`
  containing exactly one word: PASS, FAIL, ERROR, or SKIP.
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
  artifact directory.
- Execute setup, steps, assertions, and cleanup serially.
- Return nonzero on FAIL or ERROR. Return 0 only when every required assertion passes.
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
status.txt. WRITE status.txt FIRST as soon as you have a verdict, THEN expand
debug.md — never let debugging consume your whole budget and leave no verdict.
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
    if [ -f "$artifact_dir/status.txt" ]; then
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
#   agent-timeout     UNKNOWN + rc=124, no connectivity marker: the agent ran out
#                     of budget. DELIBERATELY NOT auto-retriable — a slow agent can
#                     equally mean the PRODUCT hung, and retrying could flake-pass a
#                     real hang (codex). Surfaced for human triage / a Phase-5
#                     scenario split instead.                              report-only
#   unknown           anything else (incl. PASS:nonzero without the exact provider
#                     marker, or a partial PASS/SKIP report with rc=124 —
#                     inconsistent, NOT an unambiguous infra retry)
#                                                                            NEVER retry
# Keying on the PARSED status=UNKNOWN (not merely "no status.txt") is deliberate:
# a partial report.md verdict + rc=124 is an inconsistent agent result, not an
# infra timeout. Pure (args only) => host-testable (gui-retry-classify.bats).
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
    local scenario=$1 provided=${2:-} rel vm prompt log_path status agent_rc frc=0 own=0 vm_live=1 t0 t1 t2 ta0 ta1 gate_name lane
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
    local slug scratch
    slug=$(safe_name "$rel")
    prompt="$RDIR/agent-notes/$slug.prompt.md"
    log_path="$RDIR/gui/$slug.agent.log"
    mkdir -p "$(dirname "$log_path")"
    # Per-scenario isolated scratch dir (host) + slug (for guest scratch on a
    # shared session VM). Passed to the agent's env at run_agent_command so a
    # scenario routes scratch here instead of a collision-prone fixed /tmp path.
    scratch=$(scenario_scratch_dir gui "$slug")
    mkdir -p "$scratch"
    write_agent_prompt "$vm" "$scenario" "$prompt" "" "$scratch" "$slug"
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
    VMNAME="$vm" QCI_SCENARIO_TMPDIR="$scratch" QCI_SCENARIO_SLUG="$slug" \
        run_agent_command "$prompt" "$log_path"
    agent_rc=$?
    ta1=$(date +%s)
    record_host_load gui "$rel" end
    local adir
    adir="$RDIR/gui/$(safe_name "$rel")"
    status=$(agent_artifact_status "$adir" "$log_path")
    # Fail-closed status/rc mapping (see gui_agent_verdict). UNKNOWN:0 — an agent
    # that exited 0 without rendering a usable verdict — is a hard failure here,
    # not the silent pass it used to be.
    local verdict note
    IFS=$'\t' read -r verdict note < <(gui_agent_verdict "$status" "$agent_rc")
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
                local vmN logN adirN scratchN tsa tsb statusN verdictN noteN classifierN transportN toolingN apiN
                logN="${log_path_base%.agent.log}.retry${attempt}.agent.log"
                adirN="${adir%.retry*}.retry${attempt}"
                mkdir -p "$adirN"
                # Fresh host scratch PER RETRY so stale scratch from the failed
                # attempt can't leak into the retry (mirrors the per-attempt
                # logN/adirN discipline).
                scratchN=$(scenario_scratch_dir gui "${slug}-retry${attempt}")
                mkdir -p "$scratchN"
                vmN=$(acquire_vm "$gate_name" "")
                if [ -z "$vmN" ]; then
                    log "agent scenario $rel: retry $attempt VM provision failed; keeping the previous verdict"
                    record_flake "$rel" "$first_classifier" "$first_status" "$agent_rc1" "" "$ordinal" retry-vm-provision-failed "$log_path_base"
                    provision_failed=1
                    break
                fi
                vm=$vmN; vm_live=1
                write_agent_prompt "$vmN" "$scenario" "$prompt" "$adirN" "$scratchN" "$slug"
                install_gui_waiters "$vmN" || log "agent scenario $rel: waiter-lib delivery failed (continuing)"
                suppress_idle_lock "$vmN"
                record_host_load gui "$rel" start
                tsa=$(date +%s)
                VMNAME="$vmN" QCI_SCENARIO_TMPDIR="$scratchN" QCI_SCENARIO_SLUG="$slug" \
                    run_agent_command "$prompt" "$logN"
                agent_rc=$?; tsb=$(date +%s)
                record_host_load gui "$rel" end
                statusN=$(agent_artifact_status "$adirN" "$logN")
                transportN=0; toolingN=0; apiN=0; classifierN=""; local extnetN=0
                IFS=$'\t' read -r verdictN noteN < <(gui_agent_verdict "$statusN" "$agent_rc")
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

# Record the agent identity (H6a) into manifest.txt: the sanitized QCI_AGENT_CMD
# template, the model (QCI_AGENT_MODEL, parsed from `--model X`/`-m X`, or the
# Haiku default), and a
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
    kv qci_agent_model "${model:-haiku}"
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
        local tier_base_skip app_deps_skip
        tier_base_skip=$(gui_scenario_tier_base_skip_reason "$rel" \
            "$tier5_base" "$tier4_base" "$tier5_optin" "$tier4_optin")
        app_deps_skip=$(gui_scenario_app_deps_skip_reason "$rel" "$app_deps")
        if [ "${QCI_GUI_RUN_LEGACY_QDWIN_MD:-0}" != 1 ] && gui_scenario_uses_legacy_ctrl "$scenario"; then
            skip_reason="legacy qdshell.py ctrl-socket scenario not supported by the Quickshell qdshell session"
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
