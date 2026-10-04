#!/usr/bin/env bats
# Live P7 leftover evidence for the presentation snapshot (09 item 3):
# both tier-2 home modes with the first-party qfileman image, keep-id
# owner and managed-source resolution inside the container, live follow
# of a host publish, compositor device scale 2 and 1 with the UI font in
# points, and the four installed apps importable. Host-only
# presentation-delivery.bats cannot prove these.

load helpers

teardown_file() {
    reap_vm_drivers
}

@test "live named and disposable tier-2 homes bind presentation with keep-id owner" {
    stage_vm_driver "probes/presentation-live.sh"
    vm_run "curl -fsS -o /tmp/presentation-live.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/presentation-live.sh && chmod +x /tmp/presentation-live.sh && bash /tmp/presentation-live.sh"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "presentation live probe skipped; missing installer, image, or compositor is a bake failure"
    fi
    assert_output_contains "PASS: broker allow rules loaded for named and disposable spawns"
    assert_output_contains "PASS: admin published initial snapshot"
    assert_output_contains "PASS: named (untemplated) container pres-live-named running"
    assert_output_contains "PASS: named: presentation bind source is the host public directory"
    assert_output_contains "PASS: named: presentation bind is read-only"
    assert_output_contains "PASS: named: presentation dir owner inside container is keep-id admin uid"
    assert_output_contains "PASS: named: container write into presentation directory denied"
    assert_output_contains "PASS: named: container /var/lib/qdistro exposes only presentation"
    assert_output_contains "PASS: named: first-party qfileman is importable inside the container"
    assert_output_contains "PASS: named: in-container SDK reads the managed snapshot as admin uid 1000"
    assert_output_contains "PASS: named: running in-container controller followed a host publish"
    assert_output_contains "PASS: disposable container"
    assert_output_contains "PASS: disposable: presentation bind source is the host public directory"
    assert_output_contains "PASS: disposable: presentation bind is read-only"
    assert_output_contains "PASS: disposable: presentation dir owner inside container is keep-id admin uid"
    assert_output_contains "PASS: disposable: container write into presentation directory denied"
    assert_output_contains "PASS: disposable: container /var/lib/qdistro exposes only presentation"
    assert_output_contains "PASS: disposable: first-party qfileman is importable inside the container"
    assert_output_contains "PASS: disposable: in-container SDK reads the managed snapshot as admin uid 1000"
    assert_output_contains "PASS: disposable: running in-container controller followed a host publish"
    assert_output_contains "PASS: live named and disposable presentation binds held"
}

@test "live compositor applies user scale once in points at scale 2 and 1" {
    stage_vm_driver "probes/presentation-scale.py"
    stage_vm_driver "probes/presentation-scale.sh"
    vm_run "test -S /run/user/1000/wayland-1"
    require "outer compositor not running (wayland-1 missing)"
    vm_run "curl -fsS -o /tmp/presentation-scale.py http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/presentation-scale.py && curl -fsS -o /tmp/presentation-scale.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/presentation-scale.sh && chmod +rx /tmp/presentation-scale.py /tmp/presentation-scale.sh && bash /tmp/presentation-scale.sh /tmp/presentation-scale.py"
    assert_success
    for scale in 2 1; do
        assert_output_contains "PASS: compositor output"
        assert_output_contains "PASS: [scale $scale] devicePixelRatio matches compositor scale $scale"
        assert_output_contains "PASS: [scale $scale] qt platform=wayland"
        assert_output_contains "PASS: [scale $scale] installed qdistro_presentation imports without PYTHONPATH"
        assert_output_contains "PASS: [scale $scale] controller applied the published snapshot"
        assert_output_contains "PASS: [scale $scale] user scale applied once in points"
        assert_output_contains "PASS: [scale $scale] UI font was not multiplied by devicePixelRatio"
    done
    for mod in qfileman qterminator QTermWidget qdbrowser qnotebook; do
        assert_output_contains "PASS: [scale 1] import $mod"
    done
}
