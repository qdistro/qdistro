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
#   - PRESENT-BUT-BROKEN is NOT pre-skipped: when the outer stack (wayland-1 +
#     nested KVM) IS present, the scenario RUNS even if the baked guest image is
#     absent — the agent then reports ERROR/INFRA per the scenarios' own "do not
#     silently skip" contract. Image presence is deliberately NOT a gate input.
#
# Helper signature: gui_scenario_skip_reason rel legacy nested qdshell ssh skip_qdwin

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

@test "gui run: tier-5 cold-start (20) RUNS when outer stack present (image gap is agent ERROR, not skip)" {
    # qdshell + nested KVM present; the baked image is irrelevant to the gate —
    # this is present-but-broken, the agent must report ERROR. Function must NOT
    # pre-skip it.
    run gui_scenario_skip_reason \
        "qdistro/tests/integration/permissions-gui/20-tier5-vm-cold-start.md" \
        0 1 1 ""
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

    run gui_scenario_requires_qdwin "qdistro/tests/integration/permissions-gui/56-tier4-rdp-window-visible.md"
    [ "$status" -eq 0 ]
}

@test "gui classify: admin rows do not require the qdwin profile" {
    run gui_scenario_requires_qdwin "qdistro/tests/integration/permissions-gui/03-qt-admin-app-visual.md"
    [ "$status" -ne 0 ]
}

@test "gui run: legacy qdwin md RUNS when legacy ctrl-socket present" {
    run gui_scenario_skip_reason "qdwin/tests/gui/01-foo.md" 1 1 1 "2222"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
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
