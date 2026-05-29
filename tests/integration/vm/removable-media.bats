#!/usr/bin/env bats
# Removable-media brokered mount/unmount end-to-end.
#
# Exercises qdistro-media-exec (the root socket helper) +
# org.qdistro.AdminBroker1 together, asserting the security contract in
# doc/removable-media-design.md:
#   - device strings carrying shell metacharacters / non-/dev paths are
#     refused by the helper BEFORE any broker call;
#   - a qdistro.media.mount:* allow rule lets a brokered mount succeed
#     (udisks2 mounts a loopback device under /run/media);
#   - unmount is a DISTINCT action not covered by the mount rule;
#   - a deny rule blocks the mount.
#
# The driver lives at tests/integration/vm/s60-removable-media.sh and is
# pushed into the VM by the same http-staging dance the other phaseN
# bats use (vm_exec qga quoting fragility → run-from-script).

load helpers

setup() {
    vm_run "systemctl is-active --quiet qdistro-admin-broker.service \
            || systemctl start qdistro-admin-broker.service"
}

@test "removable-media: brokered mount/unmount + injection refusal" {
    local script_path
    script_path="$(dirname "$BATS_TEST_FILENAME")/s60-removable-media.sh"
    [ -f "$script_path" ] || fail_loud "driver script not found at $script_path"

    cp "$script_path" "$(dirname "$BATS_TEST_FILENAME")/../"
    stage_http_8765 "$(dirname "$BATS_TEST_FILENAME")/.."

    vm_run 'curl -s -o /tmp/s60.sh http://10.0.2.2:8765/s60-removable-media.sh && chmod +x /tmp/s60.sh && bash /tmp/s60.sh'
    assert_success
    assert_output_contains "PASS: media: injection device string refused"
    assert_output_contains "PASS: media: non-/dev device refused"
    assert_output_contains "PASS: media: non-removable device refused even with allow rule"
    assert_output_contains "PASS: media: unmount action not covered by mount allow rule"
    assert_output_contains "PASS: media: mount denied by deny rule"
    assert_output_contains "PASS: removable-media end-to-end"
}
