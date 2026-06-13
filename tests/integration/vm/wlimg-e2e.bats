#!/usr/bin/env bats
# Workload-images e2e — the text-viewer + url-preview tier-2 WORKLOAD IMAGES on
# real podman (fable-vs-qubes workload-images milestone). These two
# open-in-disposable classes (text/plain -> text-viewer, network none;
# url-preview-known-origin -> url-preview, network egress) previously RESOLVED in
# the registry but had NO built image, so an open-in-disposable spawn failed at
# `podman image exists`. This suite proves they now BUILD via make-tier2-image
# and SPAWN cleanly through the SHIPPED /usr/bin/qdistro-tier2-spawn with the
# FULL sandbox envelope and the network mode each class declares.
#
# The heavy lifting lives in tests/integration/vm/probes/wlimg-probe.sh (staged
# to /root). It drives the SHIPPED binary + the real broker, builds the images
# from the staged source helper, and asserts: image built; open-in-disposable
# spawns; /mnt/input bound READ-ONLY; class-correct network mode (none vs
# egress); --cap-drop=ALL; no-new-privileges; read-only rootfs; the
# workload-specific seccomp profile applied.
#
# Order is load-bearing: setup_file builds both images + authors the broker
# rules + checks the compositor; each test spawns -> asserts -> tears down its
# own container; teardown_file removes the test-authored rules.

load helpers

PROBE="/root/wlimg-probe.sh"

setup_file() {
    vm_run "[ -f $PROBE ]"
    assert_success || fail_loud "probe not staged at $PROBE (deploy wlimg-probe.sh to /root)"
    vm_run "command -v podman >/dev/null"
    assert_success || fail_loud "podman not available — workload images cannot spawn"
    vm_run "[ -x /usr/bin/qdistro-tier2-spawn ]"
    assert_success || fail_loud "/usr/bin/qdistro-tier2-spawn not installed (PACKAGING GAP)"
    vm_run "[ -f /usr/libexec/qdistro/qdistro_disposable_classes.py ]"
    assert_success || fail_loud "class registry resolver not installed (PACKAGING GAP)"
    vm_run "[ -f /etc/qdistro/disposable-classes.toml ]"
    assert_success || fail_loud "class registry not installed (PACKAGING GAP)"
    vm_run "bash $PROBE setup"
    assert_success
    assert_output_contains "PASS: make-tier2-image built qdistro/tier2-text-viewer:latest"
    assert_output_contains "PASS: make-tier2-image built qdistro/tier2-url-preview:latest"
    assert_output_contains "PASS: workload-specific seccomp profiles present on the spawn search path"
    assert_output_contains "PASS: setup"
}

teardown_file() {
    vm_run "bash $PROBE teardown"
    assert_success || fail_loud "wlimg-probe teardown failed (test-authored broker rules may persist)"
    assert_output_contains "PASS: teardown"
}

@test "text-viewer: text/plain class spawns a disposable with RO /mnt/input, network=none, full sandbox envelope" {
    vm_run "bash $PROBE text-viewer"
    assert_success
    assert_output_contains "PASS: text-viewer: open spawned a disposable"
    assert_output_contains "PASS: text-viewer: input bound READ-ONLY at /mnt/input/note.txt"
    assert_output_contains "PASS: text-viewer: network=none enforced (only lo)"
    assert_output_contains "PASS: text-viewer: --read-only rootfs"
    assert_output_contains "PASS: text-viewer-envelope"
    assert_output_contains "PASS: text-viewer"
}

@test "url-preview: url-preview-known-origin class spawns a disposable with RO /mnt/input, egress network, full sandbox envelope" {
    vm_run "bash $PROBE url-preview"
    assert_success
    assert_output_contains "PASS: url-preview: open spawned a disposable"
    assert_output_contains "PASS: url-preview: input bound READ-ONLY at /mnt/input/page.url"
    assert_output_contains "PASS: url-preview: egress network attached"
    assert_output_contains "PASS: url-preview: --read-only rootfs"
    assert_output_contains "PASS: url-preview-envelope"
    assert_output_contains "PASS: url-preview"
}
