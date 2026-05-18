#!/usr/bin/env bats
# §Phase-9 (Phase 5 broker-aware Services from noctalia adoption plan)
#
# Exercises the broker D-Bus surfaces qdshell's HooksGate.qml and
# Notifications.qml speak to:
#   - HooksGate.gate(event, script) → broker.CheckPermission with
#     action="hook.allowed:<event>" — verifies allow / deny / unknown
#     verdicts and that the unknown path enqueues a pending
#     RequestPermission entry the admin can later resolve.
#   - Notifications.audit(notif) → broker.RecordNotification(s,s,s,i)
#     — verifies audit row written, action="notification.posted",
#     decision=True, urgency labels, field truncation (app:128,
#     summary:256, body:512), unicode preservation, burst smoke.
#
# Single big run-from-script bat so vm-exec's qga JSON quoting
# fragility doesn't mangle nested busctl invocations
# (memory: vm_exec_quoting_fragility.md).
#
# The driver lives at tests/integration/vm/s90-phase5-broker-e2e.sh
# and is pushed to /tmp/ inside the VM by the bats setup.

load helpers

setup() {
    vm_run "systemctl is-active --quiet qdistro-admin-broker.service \
            || systemctl start qdistro-admin-broker.service"
}

@test "phase9-broker-e2e: HooksGate + Notifications round-trip" {
    # Push the driver into the VM via the http-staging dance other
    # phaseN bats already use.
    local script_path
    script_path="$(dirname "$BATS_TEST_FILENAME")/s90-phase5-broker-e2e.sh"
    [ -f "$script_path" ] || fail_loud "driver script not found at $script_path"

    # Stage on host http server (port 8765 by convention). See
    # stage_http_8765 in helpers.bash for the kill-and-respawn rationale.
    cp "$script_path" "$(dirname "$BATS_TEST_FILENAME")/../"
    stage_http_8765 "$(dirname "$BATS_TEST_FILENAME")/.."

    vm_run 'curl -s -o /tmp/s90.sh http://10.0.2.2:8765/s90-phase5-broker-e2e.sh && chmod +x /tmp/s90.sh && bash /tmp/s90.sh'
    assert_success
    assert_output_contains "PASS: hooks: CheckPermission unknown when no rule"
    assert_output_contains "PASS: hooks: CheckPermission allow with rule"
    assert_output_contains "PASS: hooks: CheckPermission deny with rule"
    assert_output_contains "PASS: hooks: RequestPermission returns request id"
    assert_output_contains "PASS: hooks: GetPending includes the queued action"
    assert_output_contains "PASS: notifications: ListHistory contains uniquely-tagged write"
    assert_output_contains "PASS: notifications: action='notification.posted' in audit row"
    assert_output_contains "PASS: notifications: critical urgency labeled"
    assert_output_contains "PASS: notifications: low urgency labeled"
    assert_output_contains "PASS: notifications: out-of-range urgency 999 normalized to normal"
    assert_output_contains "PASS: notifications: app name truncated to exactly 128 chars"
    assert_output_contains "PASS: notifications: summary truncated to exactly 256 chars"
    assert_output_contains "PASS: notifications: body truncated to exactly 512 chars"
    assert_output_contains "PASS: notifications: 50-in-a-row burst all land in ListHistory"
    assert_output_contains "PASS: notifications: unicode preserved in audit (octal-escaped)"
    assert_output_contains "PASS: notifications: caller_uid attribution row written"
    assert_output_contains "PASS: phase9 broker round-trip end-to-end"
}
