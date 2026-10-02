#!/usr/bin/env bats
# §P07 — the broker D-Bus surface behind the admin app's Rules tab, History
# tab, tray badge, approve/deny actions, rule editor and scope rejection.
#
# This file does NOT drive the admin app: the driver never starts it and
# sends no key or click, so the PASS strings name broker behaviour only (they
# used to claim "Ctrl+Y approves", "tray badge shows", "shows modal error").
# The UI paths are proved by the GUI scenarios permissions-gui/04 and 06 and
# by tests/unit/test_admin_error_scope_ux.py.
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

@test "P07-admin-app-polish: broker Rules/History/pending/decide/SaveRule/scope-rejection surface" {
    stage_vm_driver "s104-admin-app-polish.sh"
    vm_run "curl -fsS -o /tmp/s104.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s104-admin-app-polish.sh && chmod +x /tmp/s104.sh && bash /tmp/s104.sh"
    assert_success

    # Every load-bearing PASS string of the driver.
    assert_output_contains "PASS: broker ListRules returns an on-disk YAML rule"
    assert_output_contains "PASS: broker SaveRule (as admin) writes a rule that ListRules returns"
    assert_output_contains "PASS: broker ListHistory(100) returns the audit row of a decided request"
    assert_output_contains "PASS: broker GetPending counts a newly queued request"
    assert_output_contains "PASS: broker DecideRequest(allow) as admin removes the request from GetPending"
    assert_output_contains "PASS: broker DecideRequest(deny) as admin removes the request from GetPending"
    assert_output_contains "PASS: broker SaveRule of a rule-from-request YAML is returned by ListRules"
    assert_output_contains "PASS: broker DecideRequest rejects an unknown scope with a D-Bus error"
}
