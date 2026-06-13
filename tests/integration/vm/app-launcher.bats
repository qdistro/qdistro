#!/usr/bin/env bats
# §P03 — App launcher wiring (qterminator + qnotebook + qfileman via
# org.qdistro.App1).
#
# Exercises the round-trip declared in
# plan2/tasks/P03-app-launcher.md "Success criterion": each app
# registers, qdshell PodApps lists each with its silo badge, same-silo
# send-to lands without admin, cross-silo send-to forces an admin
# approval that the audit log captures.
#
# Single big run-from-script bat — vm-exec's qga JSON quoting
# fragility mangles nested busctl invocations (see memory
# vm_exec_quoting_fragility.md), so the whole scenario runs inside
# tests/integration/vm/s102-send-to-roundtrip.sh.

load helpers

setup() {
    vm_run "systemctl is-active --quiet qdistro-admin-broker.service \
            || systemctl start qdistro-admin-broker.service"
    vm_run "systemctl is-active --quiet qdistro-session-manager.service \
            || systemctl start qdistro-session-manager.service"
}

teardown_file() {
    reap_vm_drivers
}

@test "P03-send-to-roundtrip: qterminator+qnotebook+qfileman App1 round-trip" {
    stage_vm_driver "s102-send-to-roundtrip.sh"
    vm_run "curl -fsS -o /tmp/s102.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s102-send-to-roundtrip.sh && chmod +x /tmp/s102.sh && bash /tmp/s102.sh"
    assert_success

    # Every load-bearing PASS string declared in the task file.
    assert_output_contains "PASS: qdshell PodApps lists qterminator with silo badge 'work'"
    assert_output_contains "PASS: qdshell PodApps lists qnotebook with silo badge 'work'"
    assert_output_contains "PASS: qdshell PodApps lists qfileman with silo badge 'work'"
    assert_output_contains "PASS: qterminator registered org.qdistro.App1 on session bus"
    assert_output_contains "PASS: qnotebook registered org.qdistro.App1 on session bus"
    assert_output_contains "PASS: qfileman registered org.qdistro.App1 on session bus"
    assert_output_contains "PASS: send-to from qterminator to qnotebook delivered via broker"
    assert_output_contains "PASS: qnotebook received payload (content verified)"
    assert_output_contains "PASS: qsu elevated qterminator shell (uid=0 confirmed)"
    assert_output_contains "PASS: admin approval required and logged for cross-silo send-to"
}
