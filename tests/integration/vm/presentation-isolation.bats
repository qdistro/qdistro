#!/usr/bin/env bats
# Installed presentation snapshot isolation on a live VM: public directory,
# DAC denial, SELinux label after replace, symlink rejection, last-known-good,
# live watch, and polkit/locker override ignore.
# Tier-2 mount construction is locked by presentation-delivery.bats; live
# bind+write-denial inside a container is asserted by s40-tier2-hardening.

load helpers

setup() {
    :
}

teardown_file() {
    reap_vm_drivers
}

@test "installed presentation snapshot is readable, not writable, and live-followed" {
    stage_vm_driver "probes/presentation-isolation.sh"
    vm_run "curl -fsS -o /tmp/presentation-isolation.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/probes/presentation-isolation.sh && chmod +x /tmp/presentation-isolation.sh && bash /tmp/presentation-isolation.sh"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "presentation isolation probe skipped; missing installer or package is a bake failure"
    fi

    assert_output_contains "PASS: presentation dir owner admin:admin"
    assert_output_contains "PASS: presentation dir mode 0755"
    assert_output_contains "PASS: deployment.json owned by root"
    assert_output_contains "PASS: installed import qdistro_presentation from / without PYTHONPATH"
    assert_output_contains "PASS: current.json owner admin:admin after publish"
    assert_output_contains "PASS: work user cannot create files in the presentation directory"
    assert_output_contains "PASS: work user cannot unlink current.json"
    assert_output_contains "PASS: work user cannot rename current.json"
    assert_output_contains "PASS: reader rejects a symlink current.json"
    assert_output_contains "PASS: polkit and locker ignore QDISTRO_PRESENTATION_FILE"
    assert_output_contains "PASS: deletion keeps last-known-good appearance"
    assert_output_contains "PASS: enabled:false restores fallback appearance"
    assert_output_contains "PASS: running controller follows an atomic snapshot replace"
    assert_output_contains "PASS: presentation isolation invariants held"
}
