#!/usr/bin/env bats
# Broker runtime network confinement in the runtime-only cloud VM.
# The SELinux negative control runs in the rootless native Podman builder.

load helpers

teardown_file() {
    reap_vm_drivers
}

@test "broker-no-network: runtime systemd confinement and installed unit" {
    stage_vm_driver "s56b-broker-no-network.sh"
    vm_run "curl -fsS -o /tmp/s56b-broker-no-network.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s56b-broker-no-network.sh && bash /tmp/s56b-broker-no-network.sh --runtime-only"
    assert_success
    assert_output_contains "PASS: systemd confinement denies AF_INET socket()"
    assert_output_contains "PASS: systemd confinement still permits AF_UNIX"
    assert_output_contains "PASS: live qdistro-admin-broker.service carries PrivateNetwork=yes"
    assert_output_contains "[s56b] 3 passes, 0 failures (runtime-only)"
}
