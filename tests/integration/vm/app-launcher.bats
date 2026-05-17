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

@test "P03-send-to-roundtrip: qterminator+qnotebook+qfileman App1 round-trip" {
    local script_path
    script_path="$(dirname "$BATS_TEST_FILENAME")/s102-send-to-roundtrip.sh"
    [ -f "$script_path" ] || fail_loud "driver script missing: $script_path"

    # Stage on host http server (port 8765 by convention — matches
    # broker-e2e.bats so we don't fight for a port).
    cp "$script_path" "$(dirname "$BATS_TEST_FILENAME")/../"

    if ! ss -tln 2>/dev/null | grep -q ":8765 "; then
        local stage_dir
        stage_dir="$(dirname "$BATS_TEST_FILENAME")/.."
        (cd "$stage_dir" && nohup python3 -m http.server 8765 \
            >/tmp/qdistro-bats-http.log 2>&1 &)
        sleep 1
    fi

    vm_run 'curl -s -o /tmp/s102.sh http://10.0.2.2:8765/s102-send-to-roundtrip.sh && chmod +x /tmp/s102.sh && bash /tmp/s102.sh'
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
