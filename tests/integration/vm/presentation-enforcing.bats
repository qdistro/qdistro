#!/usr/bin/env bats
# qci:enforcing — runs on a clone of the per-run golden booted
# SELinux=enforcing, reached over SSH (ci/lib/gates/bats.sh).
#
# Pack 07 scenario 4 / 09 item 4: the presentation snapshot under a real
# enforcing boot. The same isolation and tier-2 live probes that
# presentation-isolation.bats and presentation-live.bats run permissive
# must hold here, and no AVC denial may name qdistro_presentation_t.
# A permissive run of this file is a failure, never a pass.

load helpers

teardown_file() {
    reap_vm_drivers
}

@test "enforcing boot carries the presentation policy and label" {
    vm_run "getenforce"
    assert_success
    [ "$(printf '%s' "$output" | tr -d '\r' | tail -n1)" = "Enforcing" ] \
        || fail_loud "VM is not enforcing (getenforce: $output)"
    vm_run "semodule -l | grep -x qdistro_presentation"
    assert_success
    vm_run "semodule -l | grep -x qdistro_tier2"
    assert_success
    vm_run "stat -c %C /var/lib/qdistro/presentation"
    assert_success
    assert_output_contains ":qdistro_presentation_t:"
}

@test "presentation isolation holds under enforcing" {
    stage_vm_driver "probes/presentation-isolation.sh"
    vm_run "$(shell_held_cmd "curl -fsS -o /tmp/presentation-isolation.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/presentation-isolation.sh && chmod +x /tmp/presentation-isolation.sh && bash /tmp/presentation-isolation.sh")"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "presentation isolation probe skipped under enforcing: $(driver_skip_reason)"
    fi
    assert_output_contains "PASS: current.json owner admin:admin after publish"
    assert_output_contains "PASS: work user cannot create files in the presentation directory"
    assert_output_contains "PASS: work user cannot unlink current.json"
    assert_output_contains "PASS: work user cannot rename current.json"
    assert_output_contains "PASS: reader rejects a symlink current.json"
    assert_output_contains "PASS: polkit and locker ignore QDISTRO_PRESENTATION_FILE"
    assert_output_contains "PASS: running controller follows an atomic snapshot replace"
    assert_output_contains "PASS: presentation isolation invariants held"
}

@test "tier-2 qfileman homes read and follow the snapshot under enforcing" {
    stage_vm_driver "probes/presentation-live.sh"
    vm_run "$(shell_held_cmd "curl -fsS -o /tmp/presentation-live.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/presentation-live.sh && chmod +x /tmp/presentation-live.sh && bash /tmp/presentation-live.sh")"
    assert_success
    for label in named disposable; do
        assert_output_contains "PASS: $label: private runtime matches container MCS label"
        assert_output_contains "PASS: $label: presentation bind is read-only"
        assert_output_contains "PASS: $label: presentation dir owner inside container is keep-id admin uid"
        assert_output_contains "PASS: $label: container write into presentation directory denied"
        assert_output_contains "PASS: $label: in-container SDK reads the managed snapshot as admin uid 1000"
        assert_output_contains "PASS: $label: in-container inotify watch on the directory and current.json"
        assert_output_contains "PASS: $label: running in-container controller followed a host publish"
    done
    for label in named disposable; do
        assert_output_contains "PASS: $label: inner weston up and qfileman running"
        assert_output_contains "PASS: $label: inner weston and qfileman still running after checks"
    done
    assert_output_contains "PASS: shared host socket, library and presentation labels unchanged"
    assert_output_contains "PASS: concurrent containers have distinct private MCS labels"
    assert_output_contains "PASS: live named and disposable presentation binds held"
}

@test "binding-resolved tier-2 silo runs inner weston and app under enforcing" {
    # Reuse the production root-launcher + binding-resolver probe. Its secctx
    # assertions are retained, with readiness/liveness and home-write checks.
    local probe=/root/tier2-silo-secctx-wiretag-probe.sh
    vm_run "test -f $probe && bash $probe setup && bash $probe wiretag"
    local probe_status=$status probe_output=$output
    vm_run "bash $probe teardown"
    assert_success
    [ "$probe_status" -eq 0 ] || fail_loud "enforcing silo probe: $probe_output"
    output=$probe_output
    assert_output_contains "PASS: qdwin received the SILO secctx app_id ON THE WIRE"
    assert_output_contains "PASS: silo inner weston up and weston-terminal running"
    assert_output_contains "PASS: binding-resolved silo home is writable"
    assert_output_contains "PASS: silo inner weston and weston-terminal still running after checks"
    assert_output_contains "PASS: systemctl stop -> admin container gone"
}

@test "no AVC denial names qdistro_presentation_t since boot" {
    # Runs after the probes above (bats keeps file order), so their reads,
    # watches and publishes have happened under enforcing. avc-denials.sh
    # fails closed (exit 3) when ausearch or auditd is unusable.
    stage_vm_driver "probes/avc-denials.sh"
    vm_run "curl -fsS -o /tmp/avc-denials.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/avc-denials.sh && bash /tmp/avc-denials.sh 'qdistro_presentation_t'"
    echo "$output"
    [ "$status" -eq 0 ] || fail_loud "presentation AVC check failed (status=$status)"
    assert_output_contains "AVC-CLEAN"
}

@test "no AVC denial names the tier-2 workload domain since boot" {
    stage_vm_driver "probes/avc-denials.sh"
    vm_run "curl -fsS -o /tmp/avc-denials.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/avc-denials.sh && bash /tmp/avc-denials.sh ':(container_t|qdistro_tier2_t):'"
    echo "$output"
    [ "$status" -eq 0 ] || fail_loud "tier-2 AVC check failed (status=$status)"
    assert_output_contains "AVC-CLEAN"
}
