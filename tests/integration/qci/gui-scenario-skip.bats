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
#     This gate runs BEFORE the qdwin-routing bypass so it fires for these
#     qdwin-required scenarios in the default lane.
#
# Helper signatures:
#   gui_scenario_skip_reason           rel legacy nested qdshell ssh skip_qdwin
#   gui_scenario_tier_base_skip_reason rel tier5_base tier4_base tier5_optin tier4_optin

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    # gui.sh is a sourced module (function definitions only, no top-level
    # execution); pull it in so the pure helper is callable on the host.
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/gui.sh"
}

@test "gui XWayland lane: qterminal TUI scenarios skip unless explicitly opted in" {
    run gui_scenario_xwayland_skip_reason \
        "qdistro/tests/integration/permissions-gui/05-tui-help-overlay.md" 0
    [ "$status" -eq 0 ]
    [[ "$output" == *"QCI_XWAYLAND_E2E=1"* ]]

    run gui_scenario_xwayland_skip_reason \
        "qdistro/tests/integration/permissions-gui/05-tui-help-overlay.md" 1
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "gui XWayland lane: native GUI scenarios remain enabled" {
    run gui_scenario_xwayland_skip_reason \
        "qdistro/tests/integration/permissions-gui/04-qt-admin-app-approve.md" 0
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# All outer-stack capabilities present (a fully provisioned tier-4/5 GUI VM).
reason_full_stack() {
    local rel=$1
    gui_scenario_skip_reason "$rel" 1 1 1 "2222"
}

# ---------------------------------------------------------------------------
# tier-5 scenarios (20, 21)
# ---------------------------------------------------------------------------

@test "gui skip: tier-5 cold-start (20) SKIPs when wayland-1/qdshell absent" {
    # rel legacy nested qdshell ssh
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/20-tier5-vm-cold-start.md" \
        0 1 0 ""
    [ "$status" -eq 0 ]
    [ -n "$output" ]
    [[ "$output" == *"tier-5 outer stack not provisioned"* ]]
    [[ "$output" == *"wayland-1"* ]]
}

@test "gui skip: tier-5 cold-start (20) SKIPs when nested KVM absent" {
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/20-tier5-vm-cold-start.md" \
        0 0 1 ""
    [ "$status" -eq 0 ]
    [[ "$output" == *"nested KVM"* ]]
}

@test "gui run: tier-5 cold-start (20) RUNS (stack-presence gate) when outer stack present" {
    # Stack-presence function only: qdshell + nested KVM present => no stack skip.
    # (Base-image opt-in is a separate gate, gui_scenario_tier_base_skip_reason.)
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/20-tier5-vm-cold-start.md" \
        0 1 1 ""
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# gui_scenario_tier_base_skip_reason: opt-in base-image gate (runs before the
# qdwin-routing bypass in the dispatch loop).
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
        0 0 0 ""
    [ "$status" -eq 0 ]
    [ -n "$output" ]
    [[ "$output" == *"tier-5 outer stack not provisioned"* ]]
}

@test "gui run: tier-5 close-cleanup (21) RUNS when outer stack present" {
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/21-tier5-close-cleanup.md" \
        0 1 1 ""
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# ---------------------------------------------------------------------------
# qdwin app-compatibility deps gate (gui_scenario_app_deps_skip_reason)
# ---------------------------------------------------------------------------

@test "gui app-deps skip: apps/04 SKIPs when the golden lacks app deps (app_deps=0)" {
    run gui_scenario_app_deps_skip_reason \
        "qdwin/tests/apps/04-cursor-spam-suppressed.md" 0
    [ "$status" -eq 0 ]
    [[ "$output" == *"qdwin app-test deps not installed"* ]]
    [[ "$output" == *"QDWIN_APP_DEPS=1"* ]]
}

@test "gui app-deps run: apps/04 RUNS when app deps are present (app_deps=1)" {
    run gui_scenario_app_deps_skip_reason \
        "qdwin/tests/apps/04-cursor-spam-suppressed.md" 1
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "gui app-deps run: apps/05 gtk4 also gated on app deps" {
    run gui_scenario_app_deps_skip_reason \
        "qdwin/tests/apps/05-gtk4-gnome-text-editor.md" 0
    [ "$status" -eq 0 ]
    [[ "$output" == *"qdwin app-test deps not installed"* ]]
}

@test "gui app-deps run: a non-apps scenario is never app-deps-skipped" {
    run gui_scenario_app_deps_skip_reason \
        "qdwin/tests/gui/12-bar-no-overdraw.md" 0
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "gui app-deps run: default arg treats missing flag as absent (skip)" {
    run gui_scenario_app_deps_skip_reason \
        "qdwin/tests/apps/07-qt5-vlc.md"
    [ "$status" -eq 0 ]
    [[ "$output" == *"qdwin app-test deps not installed"* ]]
}

# ---------------------------------------------------------------------------
# tier-4 scenarios (56, 57)
# ---------------------------------------------------------------------------

@test "gui skip: tier-4 rdp-window (56) SKIPs when wayland-1/qdshell absent" {
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/56-tier4-rdp-window-visible.md" \
        0 1 0 ""
    [ "$status" -eq 0 ]
    [[ "$output" == *"tier-4 outer stack not provisioned"* ]]
    [[ "$output" == *"wayland-1"* ]]
}

@test "gui skip: tier-4 rdp-window (56) SKIPs when nested KVM absent" {
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/56-tier4-rdp-window-visible.md" \
        0 0 1 ""
    [ "$status" -eq 0 ]
    [[ "$output" == *"nested KVM"* ]]
}

@test "gui run: tier-4 rdp-window (56) RUNS when outer stack present (image gap is agent INFRA, not skip)" {
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/56-tier4-rdp-window-visible.md" \
        0 1 1 ""
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
        0 0 0 ""
    [ "$status" -eq 0 ]
    [ -n "$output" ]
    [[ "$output" == *"tier-4 outer stack not provisioned"* ]]
}

@test "gui run: tier-4 close-cleanup (57) RUNS when outer stack present" {
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/57-tier4-rdp-close-cleanup.md" \
        0 1 1 ""
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# ---------------------------------------------------------------------------
# qdshell-session scenarios (18, 19) — wayland-1 gate only
# ---------------------------------------------------------------------------

@test "gui skip: podapps-launcher-badge (18) SKIPs when qdshell inactive" {
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/18-podapps-launcher-badge.md" \
        0 1 0 ""
    [ "$status" -eq 0 ]
    [[ "$output" == *"qdshell session not active"* ]]
}

@test "gui run: podapps-launcher-badge (18) RUNS when qdshell active" {
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/18-podapps-launcher-badge.md" \
        0 1 1 ""
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "gui skip: tier5-loopback-visible (19) SKIPs when qdshell inactive" {
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/19-tier5-loopback-visible.md" \
        0 1 0 ""
    [ "$status" -eq 0 ]
    [[ "$output" == *"qdshell session not active"* ]]
}

@test "gui run: tier5-loopback-visible (19) RUNS when qdshell active (no nested-KVM gate)" {
    # 19 explicitly does NOT need nested KVM; a wayland-1 session is enough.
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/19-tier5-loopback-visible.md" \
        0 0 1 ""
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# ---------------------------------------------------------------------------
# legacy + ssh-only gates preserved through the refactor
# ---------------------------------------------------------------------------

@test "gui skip: legacy qdwin md SKIPs when legacy ctrl-socket absent" {
    run gui_scenario_skip_reason "qdwin/tests/gui/01-foo.md" 0 1 1 "2222"
    [ "$status" -eq 0 ]
    [[ "$output" == *"legacy qdshell ctrl-socket not available"* ]]
}

@test "gui skip: qdwin md SKIPs when qdwin lane is disabled" {
    run gui_scenario_skip_reason "qdwin/tests/gui/01-foo.md" 1 1 1 "2222" 1
    [ "$status" -eq 0 ]
    [[ "$output" == *"QCI_GUI_SKIP_QDWIN=1"* ]]
}

@test "gui classify: qdwin rows require the qdwin profile" {
    run gui_scenario_requires_qdwin "qdwin/tests/gui/01-foo.md"
    [ "$status" -eq 0 ]

    run gui_scenario_requires_qdwin "qdistro/tests/integration/qdwin-noctalia/01-bar-visible.md"
    [ "$status" -eq 0 ]

    run gui_scenario_requires_qdwin "tests/integration/qdwin-noctalia/01-bar-visible.md"
    [ "$status" -eq 0 ]

    run gui_scenario_requires_qdwin "qdistro/tests/integration/permissions-gui/56-tier4-rdp-window-visible.md"
    [ "$status" -eq 0 ]

    run gui_scenario_requires_qdwin "tests/integration/permissions-gui/20-tier5-vm-cold-start.md"
    [ "$status" -eq 0 ]
}

@test "gui classify: admin rows do not require the qdwin profile" {
    run gui_scenario_requires_qdwin "qdistro/tests/integration/permissions-gui/03-qt-admin-app-visual.md"
    [ "$status" -ne 0 ]
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
    run gui_scenario_requires_qdwin "$output"
    [ "$status" -eq 0 ]
}

@test "gui classify: qdistro worktree path keeps qdistro identity" {
    local fixture="$BATS_TEST_TMPDIR/qdistro-worktree"
    mkdir -p "$fixture/tests/integration/permissions-gui"
    touch "$fixture/tests/integration/permissions-gui/18-podapps-launcher-badge.md"

    QDISTRO_REPO="$fixture"
    run gui_scenario_rel "$fixture/tests/integration/permissions-gui/18-podapps-launcher-badge.md"
    [ "$status" -eq 0 ]
    [ "$output" = "qdistro/tests/integration/permissions-gui/18-podapps-launcher-badge.md" ]
    run gui_scenario_requires_qdwin "$output"
    [ "$status" -eq 0 ]
}

@test "gui run: legacy qdwin md RUNS when legacy ctrl-socket present" {
    run gui_scenario_skip_reason "qdwin/tests/gui/01-foo.md" 1 1 1 "2222"
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

@test "gui run: admin permissions scenario still RUNS when qdwin lane is disabled" {
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/03-qt-admin-app-visual.md" \
        0 0 0 "" 1
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "gui skip: SELinux (55) SKIPs when VM_SSH_PORT unset" {
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/55-qsu-selinux-enforcing.md" \
        1 1 1 ""
    [ "$status" -eq 0 ]
    [[ "$output" == *"VM_SSH_PORT not set"* ]]
}

@test "gui run: SELinux (55) RUNS when VM_SSH_PORT set" {
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/55-qsu-selinux-enforcing.md" \
        1 1 1 "2222"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

# ---------------------------------------------------------------------------
# Unknown / ungated scenario always runs
# ---------------------------------------------------------------------------

@test "gui run: an ungated permissions-gui scenario is never pre-skipped" {
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/03-qt-admin-app-visual.md" \
        0 0 0 ""
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}
