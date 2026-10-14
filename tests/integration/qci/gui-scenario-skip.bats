#!/usr/bin/env bats
#
# Host-only unit tests for the GUI gate's stack-absent SKIP verdict logic
# (ci/lib/gates/gui.sh::gui_scenario_skip_reason). NO VM is booted: the
# function is pure (reads only its arguments), which is the whole point of the
# fix — the SKIP-vs-run decision is host-testable even when the GUI VM stack is
# absent. See todo/ci-triage-20260616/03-gui-tier4-tier5-skip-gap.md.
#
# Contract under test:
#   - tier-4/tier-5 GUI scenarios (permissions-gui/20,21,56,57) resolve to SKIP
#     when the OUTER stack is unprovisioned (no qdwin/qdshell wayland-1, OR no
#     nested KVM) — mirroring the bats tiered-isolation skip — instead of being
#     dispatched to the agent (which then writes ERROR, the bug).
#   - tier-4/5 base image is OPT-IN: when the outer stack is present but the
#     opt-in base image is absent AND the run did not opt in
#     (QDISTRO_BUILD_TIER{4,5}_BASE=1), gui_scenario_tier_base_skip_reason
#     resolves to SKIP (cheap + honest). When the run DID opt in but the bake is
#     still missing/broken, the scenario RUNS and the agent reports ERROR/INFRA
#     per the scenarios' own "do not silently skip a requested bake" contract.
#     This gate applies to disposable runs too (they never consult the
#     stack-presence function: their workers have the outer stack).
#
# Helper signatures:
#   gui_scenario_skip_reason           rel nested qdshell   (explicit --vm only)
#   gui_scenario_tier_base_skip_reason rel tier5_base tier4_base tier5_optin tier4_optin

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    # gui.sh is a sourced module (function definitions only, no top-level
    # execution); pull it in so the pure helper is callable on the host.
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/gui.sh"
}

# All outer-stack capabilities present (a fully provisioned tier-4/5 GUI VM).
reason_full_stack() {
    local rel=$1
    gui_scenario_skip_reason "$rel" 1 1
}

# ---------------------------------------------------------------------------
# tier-5 scenarios (20, 21)
# ---------------------------------------------------------------------------

@test "gui skip: tier-5 cold-start (20) SKIPs when wayland-1/qdshell absent" {
    # rel nested qdshell
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/20-tier5-vm-cold-start.md" \
        1 0
    [ "$status" -eq 0 ]
    [ -n "$output" ]
    [[ "$output" == *"tier-5 outer stack not provisioned"* ]]
    [[ "$output" == *"wayland-1"* ]]
}

@test "gui skip: tier-5 cold-start (20) SKIPs when nested KVM absent" {
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/20-tier5-vm-cold-start.md" \
        0 1
    [ "$status" -eq 0 ]
    [[ "$output" == *"nested KVM"* ]]
}

@test "gui run: tier-5 cold-start (20) RUNS (stack-presence gate) when outer stack present" {
    # Stack-presence function only: qdshell + nested KVM present => no stack skip.
    # (Base-image opt-in is a separate gate, gui_scenario_tier_base_skip_reason.)
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/20-tier5-vm-cold-start.md" \
        1 1
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# gui_scenario_tier_base_skip_reason: opt-in base-image gate (applies to
# disposable runs too).
# args: rel tier5_base tier4_base tier5_optin tier4_optin
@test "gui tier-base skip: tier-5 (20) SKIPs when base image absent and NOT opted-in" {
    run gui_scenario_tier_base_skip_reason \
        "qdistro/tests/integration/permissions-gui/20-tier5-vm-cold-start.md" \
        0 0 0 0
    [ "$status" -eq 0 ]
    [[ "$output" == *"tier-5 base image not built"* ]]
}

@test "gui tier-base run: tier-5 (20) RUNS (ERROR contract) when base absent but opted-in" {
    run gui_scenario_tier_base_skip_reason \
        "qdistro/tests/integration/permissions-gui/20-tier5-vm-cold-start.md" \
        0 0 1 0
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "gui tier-base run: tier-5 (20) RUNS when base image present" {
    run gui_scenario_tier_base_skip_reason \
        "qdistro/tests/integration/permissions-gui/20-tier5-vm-cold-start.md" \
        1 1 0 0
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "gui tier-base run: tier-4 (57) SKIPs when base absent and NOT opted-in" {
    run gui_scenario_tier_base_skip_reason \
        "qdistro/tests/integration/permissions-gui/57-tier4-rdp-close-cleanup.md" \
        1 0 0 0
    [ "$status" -eq 0 ]
    [[ "$output" == *"tier-4 base image not built"* ]]
}

@test "gui tier-base run: non-tier scenario never tier-base-skipped" {
    run gui_scenario_tier_base_skip_reason \
        "qdistro/tests/integration/permissions-gui/18-podapps-launcher-badge.md" \
        0 0 0 0
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "gui run: tier-5 cold-start (20) RUNS when whole stack present" {
    run reason_full_stack \
        "qdistro/tests/integration/permissions-gui/20-tier5-vm-cold-start.md"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "gui skip: tier-5 close-cleanup (21) SKIPs when outer stack absent" {
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/21-tier5-close-cleanup.md" \
        0 0
    [ "$status" -eq 0 ]
    [ -n "$output" ]
    [[ "$output" == *"tier-5 outer stack not provisioned"* ]]
}

@test "gui run: tier-5 close-cleanup (21) RUNS when outer stack present" {
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/21-tier5-close-cleanup.md" \
        1 1
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "gui tier-5 launch password prompt (63): qdwin-routed, tier-5 stack and base gates apply" {
    local rel=qdistro/tests/integration/permissions-gui/63-tier5-launch-password-prompt.md
    run gui_scenario_requires_qdwin "$rel"
    [ "$status" -eq 0 ]
    run gui_scenario_requires_qdwin "tests/integration/permissions-gui/63-tier5-launch-password-prompt.md"
    [ "$status" -eq 0 ]
    run gui_scenario_skip_reason "$rel" 0 0 1 ""
    [[ "$output" == *"tier-5 outer stack not provisioned"* ]]
    run gui_scenario_skip_reason "$rel" 0 1 1 ""
    [ -z "$output" ]
    run gui_scenario_tier_base_skip_reason "$rel" 0 0 0 0
    [[ "$output" == *"tier-5 base image not built"* ]]
    run gui_scenario_tier_base_skip_reason "$rel" 1 0 0 0
    [ -z "$output" ]
}

# ---------------------------------------------------------------------------
# gui-apps lane gate (gui_scenario_app_deps_skip_reason rel app_deps apps_optin)
# ---------------------------------------------------------------------------

@test "gui-apps lane: third-party app scenarios SKIP unless QCI_GUI_APPS=1" {
    local rel
    for rel in qdwin/tests/apps/01-firefox-max-restore.md \
               qdwin/tests/apps/05-gtk4-gnome-text-editor.md \
               qdwin/tests/apps/07-qt5-vlc.md \
               qdwin/tests/apps/09-wxwidgets-audacity.md \
               qdwin/tests/apps/11-imlib2-feh.md; do
        # Even a golden that HAS the app deps does not run them without opt-in:
        # QDWIN_APP_DEPS=1 alone must no longer pull them into qci full.
        run gui_scenario_app_deps_skip_reason "$rel" 1 0
        [ "$status" -eq 0 ]
        [[ "$output" == *"gui-apps lane"* ]] || { echo "$rel: $output"; return 1; }
        [[ "$output" == *"QCI_GUI_APPS=1"* ]]
    done
}

@test "gui-apps lane: opted in with app deps, the app scenarios RUN" {
    run gui_scenario_app_deps_skip_reason "qdwin/tests/apps/06-gtk3-thunar-xwayland.md" 1 1
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "gui-apps lane: opted in on a golden without the app set SKIPs naming the bake" {
    run gui_scenario_app_deps_skip_reason "qdwin/tests/apps/08-electron-chromium.md" 0 1
    [ "$status" -eq 0 ]
    [[ "$output" == *"app-test deps not installed"* ]]
}

@test "gui-apps lane: default args treat the lane as not opted in (skip)" {
    run gui_scenario_app_deps_skip_reason "qdwin/tests/apps/10-tk-fltk-swing.md"
    [ "$status" -eq 0 ]
    [[ "$output" == *"QCI_GUI_APPS=1"* ]]
}

@test "gui-apps lane: blocking app scenarios 02 03 04 12 13 always run" {
    local rel
    for rel in qdwin/tests/apps/02-xterm-xwayland-launch.md \
               qdwin/tests/apps/03-foot-vs-xterm-tagging.md \
               qdwin/tests/apps/04-cursor-spam-suppressed.md \
               qdwin/tests/apps/12-keystroke-roundtrip.md \
               qdwin/tests/apps/13-rdp-subscribe-frame.md; do
        run gui_scenario_app_deps_skip_reason "$rel" 0 0
        [ "$status" -eq 0 ]
        [ -z "$output" ] || { echo "$rel: $output"; return 1; }
    done
}

@test "gui-apps lane: the lane split matches the scenario files on disk" {
    # Every shipped apps scenario is either in the lane or blocking, and the
    # lane names exactly 01 and 05-11 (8 files).
    local f rel lane=0 blocking=0
    for f in "$REPO_ROOT"/qdwin/tests/apps/[0-9][0-9]-*.md; do
        rel=qdwin/tests/apps/${f##*/}
        if gui_scenario_is_gui_apps_lane "$rel"; then lane=$((lane + 1)); else blocking=$((blocking + 1)); fi
    done
    [ "$lane" -eq 8 ]
    [ "$blocking" -eq 4 ]
}

@test "gui-apps lane: a non-apps scenario is never app-deps-skipped" {
    run gui_scenario_app_deps_skip_reason \
        "qdwin/tests/gui/12-bar-no-overdraw.md" 0 0
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "agent_scenarios: workflow-gui is enumerated, deleted scenarios are not" {
    WORKSPACE=$REPO_ROOT QDISTRO_REPO=$REPO_ROOT run agent_scenarios
    [ "$status" -eq 0 ]
    [[ "$output" == *"/tests/integration/workflow-gui/04-admin-workflowstab-list-run-view.md"* ]]
    [[ "$output" == *"/tests/integration/workflow-gui/05-approval-queue-gated-run.md"* ]]
    [[ "$output" == *"/tests/integration/permissions-gui/01-tui-approver-visual.md"* ]]
    [[ "$output" == *"/tests/integration/permissions-gui/35-tui-and-qt-concurrent.md"* ]]
    [[ "$output" == *"/tests/integration/permissions-gui/40-tui-survives-broker-restart.md"* ]]
    # Merged into 01/40/12/workflow-gui-04, or replaced by qsu-enforcing.bats
    # (2026-10-14, labwc lane retired).
    local gone
    for gone in permissions-gui/02-tui-scope-picker permissions-gui/05-tui-help-overlay \
        permissions-gui/09-tui-broker-offline permissions-gui/16-realapp-sendto-visual \
        permissions-gui/55-qsu-selinux-enforcing workflow-gui/01- workflow-gui/02- workflow-gui/03-; do
        [[ "$output" != *"/tests/integration/$gone"* ]] || { echo "still enumerated: $gone"; return 1; }
    done
    [[ "$output" != *"/qdwin/tests/gui/01-"* ]]
    [[ "$output" != *"06-taskbar-isolation-menu"* ]]
    [[ "$output" != *"48-qsu-tui-argv-rendering"* ]]
}

# ---------------------------------------------------------------------------
# tier-4 scenarios (56, 57)
# ---------------------------------------------------------------------------

@test "gui skip: tier-4 rdp-window (56) SKIPs when wayland-1/qdshell absent" {
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/56-tier4-rdp-window-visible.md" \
        1 0
    [ "$status" -eq 0 ]
    [[ "$output" == *"tier-4 outer stack not provisioned"* ]]
    [[ "$output" == *"wayland-1"* ]]
}

@test "gui skip: tier-4 rdp-window (56) SKIPs when nested KVM absent" {
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/56-tier4-rdp-window-visible.md" \
        0 1
    [ "$status" -eq 0 ]
    [[ "$output" == *"nested KVM"* ]]
}

@test "gui run: tier-4 rdp-window (56) RUNS when outer stack present (image gap is agent INFRA, not skip)" {
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/56-tier4-rdp-window-visible.md" \
        1 1
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "gui run: tier-4 rdp-window (56) RUNS when whole stack present" {
    run reason_full_stack \
        "qdistro/tests/integration/permissions-gui/56-tier4-rdp-window-visible.md"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "gui skip: tier-4 close-cleanup (57) SKIPs when outer stack absent" {
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/57-tier4-rdp-close-cleanup.md" \
        0 0
    [ "$status" -eq 0 ]
    [ -n "$output" ]
    [[ "$output" == *"tier-4 outer stack not provisioned"* ]]
}

@test "gui run: tier-4 close-cleanup (57) RUNS when outer stack present" {
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/57-tier4-rdp-close-cleanup.md" \
        1 1
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# ---------------------------------------------------------------------------
# qdshell-session scenarios (18, 19) — wayland-1 gate only
# ---------------------------------------------------------------------------

@test "gui skip: podapps-launcher-badge (18) SKIPs when qdshell inactive" {
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/18-podapps-launcher-badge.md" \
        1 0
    [ "$status" -eq 0 ]
    [[ "$output" == *"qdshell session not active"* ]]
}

@test "gui run: podapps-launcher-badge (18) RUNS when qdshell active" {
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/18-podapps-launcher-badge.md" \
        1 1
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "gui skip: tier5-loopback-visible (19) SKIPs when qdshell inactive" {
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/19-tier5-loopback-visible.md" \
        1 0
    [ "$status" -eq 0 ]
    [[ "$output" == *"qdshell session not active"* ]]
}

@test "gui run: tier5-loopback-visible (19) RUNS when qdshell active (no nested-KVM gate)" {
    # 19 explicitly does NOT need nested KVM; a wayland-1 session is enough.
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/19-tier5-loopback-visible.md" \
        0 1
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# ---------------------------------------------------------------------------
# legacy + ssh-only gates preserved through the refactor
# ---------------------------------------------------------------------------

@test "gui skip: explicit-VM qdwin md SKIPs when the qdshell session is absent" {
    run gui_scenario_skip_reason "qdwin/tests/gui/01-foo.md" 1 0
    [ "$status" -eq 0 ]
    [[ "$output" == *"qdshell session not active"* ]]
}

@test "gui run: qdwin app scenarios do not require the legacy ctrl-socket" {
    # tests/apps uses qdwin-bystander + its own FIFO, not the retired qdshell
    # control socket. App dependencies are gated separately.
    run gui_scenario_skip_reason \
        "qdwin/tests/apps/04-cursor-spam-suppressed.md" 1 1
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "gui classify: canonical path behind workspace symlink keeps qdwin identity" {
    local fixture="$BATS_TEST_TMPDIR/scenario-roots"
    mkdir -p "$fixture/workspace" "$fixture/real-qdwin/tests/apps"
    ln -s "$fixture/real-qdwin" "$fixture/workspace/qdwin"
    touch "$fixture/real-qdwin/tests/apps/01-firefox.md"

    WORKSPACE="$fixture/workspace"
    QDWIN_REPO="$fixture/workspace/qdwin"
    run gui_scenario_rel "$fixture/real-qdwin/tests/apps/01-firefox.md"
    [ "$status" -eq 0 ]
    [ "$output" = "qdwin/tests/apps/01-firefox.md" ]
}

@test "gui classify: qdistro worktree path keeps qdistro identity" {
    local fixture="$BATS_TEST_TMPDIR/qdistro-worktree"
    mkdir -p "$fixture/tests/integration/permissions-gui"
    touch "$fixture/tests/integration/permissions-gui/18-podapps-launcher-badge.md"

    QDISTRO_REPO="$fixture"
    run gui_scenario_rel "$fixture/tests/integration/permissions-gui/18-podapps-launcher-badge.md"
    [ "$status" -eq 0 ]
    [ "$output" = "qdistro/tests/integration/permissions-gui/18-podapps-launcher-badge.md" ]
}

@test "gui run: explicit-VM qdwin md RUNS on a qdshell session" {
    # qci replay / qci gui --vm on a qdwin VM: the retired legacy ctrl-socket
    # probe never answered on the Quickshell session; modern scenarios run.
    run gui_scenario_skip_reason "qdwin/tests/gui/17-foo.md" 1 1
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# ---------------------------------------------------------------------------
# Content-based legacy ctrl-socket detection (gui_scenario_uses_legacy_ctrl).
# This is what actually gates the qdwin profile in normal CI: legacy qdshell.py
# scenarios are skipped by content while modern qs-ipc / app scenarios run.
# ---------------------------------------------------------------------------

@test "legacy detect: qdwin md using qdwin_ctrl IS legacy" {
    local d="$BATS_TEST_TMPDIR/qdwin/tests/gui"; mkdir -p "$d"
    printf 'run: qdwin_ctrl "list"\n' > "$d/13-foo.md"
    run gui_scenario_uses_legacy_ctrl "$d/13-foo.md"
    [ "$status" -eq 0 ]
}

@test "legacy detect: qdwin md using raw qdshell.sock socat IS legacy" {
    local d="$BATS_TEST_TMPDIR/qdwin/tests/apps"; mkdir -p "$d"
    printf 'echo max | socat - UNIX-CONNECT:/run/user/1000/qdshell.sock\n' > "$d/03-foo.md"
    run gui_scenario_uses_legacy_ctrl "$d/03-foo.md"
    [ "$status" -eq 0 ]
}

@test "legacy detect: modern qs-ipc qdwin md is NOT legacy" {
    local d="$BATS_TEST_TMPDIR/qdwin/tests/gui"; mkdir -p "$d"
    printf 'qs -p /usr/share/quickshell/qdshell ipc call ...\n' > "$d/17-foo.md"
    run gui_scenario_uses_legacy_ctrl "$d/17-foo.md"
    [ "$status" -ne 0 ]
}

@test "legacy detect: app-launch qdwin md (no ctrl-socket) is NOT legacy" {
    local d="$BATS_TEST_TMPDIR/qdwin/tests/apps"; mkdir -p "$d"
    printf 'launch feh and screenshot the window\n' > "$d/11-foo.md"
    run gui_scenario_uses_legacy_ctrl "$d/11-foo.md"
    [ "$status" -ne 0 ]
}

@test "legacy detect: non-qdwin scenario path is never legacy" {
    local d="$BATS_TEST_TMPDIR/qdistro/tests/integration/qdwin-noctalia"; mkdir -p "$d"
    printf 'qdwin_ctrl "list"\n' > "$d/03-foo.md"
    run gui_scenario_uses_legacy_ctrl "$d/03-foo.md"
    [ "$status" -ne 0 ]
}

# ---------------------------------------------------------------------------
# Unknown / ungated scenario always runs
# ---------------------------------------------------------------------------

@test "gui run: an ungated permissions-gui scenario is never pre-skipped" {
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/03-qt-admin-app-visual.md" \
        0 0
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# ---------------------------------------------------------------------------
# One GUI lane: the labwc/XWayland admin lane and its opt-ins are gone
# ---------------------------------------------------------------------------

@test "one GUI lane: every scenario worker and the session VM use the qdwin profile" {
    local gui="$REPO_ROOT/ci/lib/gates/gui.sh" vm="$REPO_ROOT/ci/lib/vm.sh"
    grep -q 'gate_name="gui-qdwin-$(safe_name "$rel")"' "$gui"
    grep -q 'svm=$(acquire_vm gui-qdwin "$explicit")' "$gui"
    grep -q 'ensure_run_golden gui-qdwin' "$gui"
    run grep -nE 'gui-admin|QCI_GUI_SKIP_QDWIN|QCI_XWAYLAND_E2E|QCI_LABWC_ADMIN_LANE|QCI_VM_GUI_SESSION|gui_scenario_requires_qdwin|labwc_lane|xwayland_skip' \
        "$gui" "$vm" "$REPO_ROOT/ci/lib/dispatch.sh" "$REPO_ROOT/ci/lib/usage.sh" \
        "$REPO_ROOT/ci/lib/bootstrap.sh" "$REPO_ROOT/ci/justfile"
    [ "$status" -ne 0 ] || { echo "lane selection left: $output" >&2; return 1; }
    # Every GUI gate name maps to the one GUI spinner and golden.
    grep -q 'gui|gui-\*) spinner=spin-test-vm-gui.sh ;;' "$vm"
    grep -q 'gui|gui-\*) golden_backing="$RUN_GOLDEN_GUI_QDWIN" ;;' "$vm"
}
