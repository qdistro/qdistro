#!/usr/bin/env bats
# Task 06 browser-rollback demo (fableplan2; doc/06-integration-tests.md). This
# suite IS the deliverable the slice exists for: update a browser silo, prove it
# still renders and fetches pages, roll it back WITH its state, prove again —
# and surface the post-update login regression that pre-promotion probes cannot
# see (the honest motivation for rollback).
#
# It drives the REAL launch path (qdistro-silo-launch -> the tier-2 launcher
# unit -> spawn-tier2 -> resolve-binding -> the task-05 state mount -> a detached
# `qdistro-silo-browserdemo` container we `podman exec` headless Chromium into),
# so it also closes the last task-05 VM-gated item. The heavy lifting is in
# tests/integration/vm/probes/templates-browser-probe.sh (+ the login fixture
# templates-browser-login-site.py), both staged to /root by fresh-vm-bootstrap.
#
# Outer-Wayland prerequisite (codex r2): there is NO standalone headless
# outer-weston fixture; the suite reuses the booted admin session's compositor
# and FAILS HARD (loudly, per tests/AGENTS.md) when /run/user/1000/wayland-1 is
# absent. Order is load-bearing: setup_file provisions the silo + builds gen A;
# baseline logs in on A; update-flip builds gen B and snapshots; the regression,
# rollback and GC scenarios depend on both generations and the A-era cookie.
#
# NB (gotcha): vm_run/vm_run_admin call bats `run` internally, so they are
# invoked BARE here — wrapping them in another `run` would capture nothing.

load helpers

PROBE="/root/templates-browser-probe.sh"

setup_file() {
    vm_run "[ -f $PROBE ]"
    assert_success || fail_loud "probe not staged at $PROBE (fresh-vm-bootstrap.sh probes step)"
    vm_run "[ -f /root/templates-browser-login-site.py ]"
    assert_success || fail_loud "login site not staged (fresh-vm-bootstrap.sh probes step)"
    vm_run_admin "command -v podman >/dev/null"
    assert_success || fail_loud "podman not available for admin in the VM"
    vm_run "systemctl is-active --quiet qdistro-session-manager.service"
    assert_success || fail_loud "qdistro-session-manager.service is not active"

    # Hard prerequisite: the booted admin compositor (the outer wayland socket
    # spawn-tier2 binds). No standalone outer-weston fixture exists; fail loud.
    start_user_session || fail_loud "admin compositor (/run/user/1000/wayland-1) is absent — this suite requires the booted admin session"

    # Provision the session silo (root: silos.yaml needs a multi-token holder
    # argv CreateTemplateSilo cannot express; the daemon reloads on restart).
    vm_run "bash $PROBE provision-silo"
    assert_success
    assert_output_contains "PASS: provision-silo"

    # Build + validate + promote the baseline generation A and start the login
    # site (slow: a real Chromium image build; run once).
    vm_run_admin "bash $PROBE setup"
    assert_success
    assert_output_contains "PASS: setup"
}

teardown_file() {
    vm_run_admin "bash $PROBE teardown" || true
    vm_run "bash $PROBE deprovision-silo" || true
}

@test "browser: baseline — launch detached, render, log in, cookie persists in state" {
    vm_run_admin "bash $PROBE baseline"
    assert_success
    assert_output_contains "PASS: baseline"
}

@test "browser: state isolation — a candidate runtime cannot reach the silo profile" {
    vm_run_admin "bash $PROBE state-isolation"
    assert_success
    assert_output_contains "PASS: state-isolation"
}

@test "browser: update flip — promote B; running stays A; restart_pending; B renders; session survives" {
    vm_run_admin "bash $PROBE update-flip"
    assert_success
    assert_output_contains "PASS: update-flip"
}

@test "browser: broken update never lands — sabotaged candidate fails validation, promote refuses" {
    vm_run_admin "bash $PROBE broken-update"
    assert_success
    assert_output_contains "PASS: broken-update"
}

@test "browser: post-update login regression — probes green, fresh login fails, A-era session still works" {
    vm_run_admin "bash $PROBE login-regression"
    assert_success
    assert_output_contains "PASS: login-regression"
}

@test "browser: breakage matrix — js-break (DOM catches it) and slow-auth (timeout, no leftovers)" {
    vm_run_admin "bash $PROBE breakage-matrix"
    assert_success
    assert_output_contains "PASS: breakage-matrix"
}

@test "browser: rollback with state — A runs again, fresh login AND restored cookie reach /home" {
    vm_run_admin "bash $PROBE rollback"
    assert_success
    assert_output_contains "PASS: rollback"
}

@test "browser: GC respects the story — A and B survive, sabotaged payload collected, evidence outlives it" {
    vm_run_admin "bash $PROBE gc"
    assert_success
    assert_output_contains "PASS: gc"
}
