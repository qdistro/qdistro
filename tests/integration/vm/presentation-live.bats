#!/usr/bin/env bats
# Live P7 leftover evidence for the presentation snapshot: both tier-2
# home modes, keep-id owner inside the container, and compositor device
# scale applied once in points. Host-only presentation-delivery.bats
# cannot prove these. Failures are load-bearing debug for a full qci.

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
    assert_output_contains "PASS: named (untemplated) container pres-live-named running"
    assert_output_contains "PASS: named: presentation bind source is the host public directory"
    assert_output_contains "PASS: named: presentation bind is read-only"
    assert_output_contains "PASS: named: presentation dir owner inside container is keep-id admin uid"
    assert_output_contains "PASS: named: container write into presentation directory denied"
    assert_output_contains "PASS: named: container /var/lib/qdistro exposes only presentation"
    assert_output_contains "PASS: disposable container"
    assert_output_contains "PASS: disposable: presentation bind source is the host public directory"
    assert_output_contains "PASS: disposable: presentation bind is read-only"
    assert_output_contains "PASS: disposable: presentation dir owner inside container is keep-id admin uid"
    assert_output_contains "PASS: disposable: container write into presentation directory denied"
    assert_output_contains "PASS: disposable: container /var/lib/qdistro exposes only presentation"
    assert_output_contains "PASS: live named and disposable presentation binds held"
}

@test "live compositor applies user scale once in points" {
    stage_vm_driver "probes/presentation-scale.py"
    vm_run "test -S /run/user/1000/wayland-1"
    require "outer compositor not running (wayland-1 missing)"
    vm_run "curl -fsS -o /tmp/presentation-scale.py http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/presentation-scale.py && chmod +x /tmp/presentation-scale.py && runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 QT_QPA_PLATFORM=wayland PYTHONSAFEPATH=1 python3 /tmp/presentation-scale.py"
    assert_success
    assert_output_contains "PASS: compositor socket exists:"
    assert_output_contains "PASS: devicePixelRatio="
    assert_output_contains "PASS: qt platform=wayland"
    assert_output_contains "PASS: installed qdistro_presentation imports without PYTHONPATH"
    assert_output_contains "PASS: controller applied the published snapshot"
    assert_output_contains "PASS: user scale applied once in points"
    assert_output_contains "PASS: UI font was not multiplied by devicePixelRatio"
    assert_output_contains "PASS: import qfileman"
    assert_output_contains "PASS: import qterminator"
    assert_output_contains "PASS: import qdbrowser"
    assert_output_contains "PASS: import qnotebook"
}
