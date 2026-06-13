#!/usr/bin/env bats
# §P07 — Admin app polish (Rules tab + History tab + tray badge +
# documented keyboard shortcuts + ScopeNotPermitted modal).
#
# Exercises the round-trip declared in
# plan2/tasks/P07-admin-app-polish.md "Success criterion": all PASS
# strings below are load-bearing — drop one and the task is incomplete.
#
# The actual scenario runs inside
# tests/integration/vm/s104-admin-app-polish.sh so vm-exec's qga JSON
# quoting can't mangle busctl payloads (same pattern as
# app-launcher.bats / s102).

load helpers

setup() {
    vm_run "systemctl is-active --quiet qdistro-admin-broker.service \
            || systemctl start qdistro-admin-broker.service"
}

teardown_file() {
    reap_vm_drivers
}

@test "P07-admin-app-polish: Rules + History + tray + shortcuts + modal" {
    stage_vm_driver "s104-admin-app-polish.sh"
    vm_run "curl -fsS -o /tmp/s104.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s104-admin-app-polish.sh && chmod +x /tmp/s104.sh && bash /tmp/s104.sh"
    assert_success

    # Every load-bearing PASS string declared in the task file.
    assert_output_contains "PASS: Rules tab shows existing rules from broker"
    assert_output_contains "PASS: Admin creates new rule via Rules tab (SaveRule called)"
    assert_output_contains "PASS: History tab shows last 100 entries"
    assert_output_contains "PASS: tray badge shows pending count"
    assert_output_contains "PASS: Ctrl+Y approves pending request"
    assert_output_contains "PASS: Ctrl+N denies pending request"
    assert_output_contains "PASS: Ctrl+R creates rule from current request"
    assert_output_contains "PASS: Alt+A approves all pending"
    assert_output_contains "PASS: Alt+D denies all pending"
    assert_output_contains "PASS: ScopeNotPermitted shows modal error"
}
