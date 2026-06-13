#!/usr/bin/env bats
# Open-in-disposable e2e — the REAL podman + real-broker half of the
# open-in-disposable flow (07-disposables-plan P2). The host lanes
# (tests/unit/test_disposable_classes.py + test_tier2_spawn.py +
# test_open_in_disposable_sdk.py) prove the registry parse, the min_tier gate,
# the trusted-path open gate, and the RO bind against fakes; this suite swaps in
# the SHIPPED /usr/bin/qdistro-tier2-spawn, the installed class registry
# (/etc/qdistro/disposable-classes.toml + /usr/libexec/qdistro/
# qdistro_disposable_classes.py), and the live admin broker — the half the
# headless dev host cannot run (rootless podman + a live compositor + a real
# system-bus broker).
#
# The heavy lifting lives in tests/integration/vm/probes/disp-open-probe.sh
# (staged to /root by fresh-vm-bootstrap.sh). It drives the SHIPPED binary (not
# a source copy) so a packaging gap surfaces.
#
# Order is load-bearing: setup_file builds the image + authors BOTH gate rules +
# checks the compositor; each test is self-contained (spawn -> assert ->
# teardown) so a stranded container never leaks; teardown_file removes the rules.

load helpers

PROBE="/root/disp-open-probe.sh"

setup_file() {
    vm_run "[ -f $PROBE ]"
    assert_success || fail_loud "probe not staged at $PROBE (fresh-vm-bootstrap.sh probes step)"
    vm_run "command -v podman >/dev/null"
    assert_success || fail_loud "podman not available — open-in-disposable cannot spawn"
    vm_run "[ -x /usr/bin/qdistro-tier2-spawn ]"
    assert_success || fail_loud "/usr/bin/qdistro-tier2-spawn not installed (PACKAGING GAP)"
    vm_run "[ -f /usr/libexec/qdistro/qdistro_disposable_classes.py ]"
    assert_success || fail_loud "class registry resolver not installed (PACKAGING GAP)"
    vm_run "[ -f /etc/qdistro/disposable-classes.toml ]"
    assert_success || fail_loud "class registry not installed (PACKAGING GAP)"
    vm_run "bash $PROBE setup"
    assert_success
    assert_output_contains "PASS: setup"
}

teardown_file() {
    vm_run "bash $PROBE teardown"
    assert_success || fail_loud "disp-open-probe teardown failed (test-authored broker rules may persist)"
    assert_output_contains "PASS: teardown"
}

@test "open-in-disposable: enabled class + allow -> disposable spawns with RO-mounted input, readable not writable" {
    vm_run "bash $PROBE open-ro-mount"
    assert_success
    assert_output_contains "PASS: open spawned a disposable"
    assert_output_contains "PASS: input bound READ-ONLY at /mnt/input/secret.txt"
    assert_output_contains "PASS: input READABLE inside the disposable (content matches)"
    assert_output_contains "PASS: input is NOT writable inside the disposable (RO enforced end-to-end)"
    assert_output_contains "PASS: open-ro-mount"
}

@test "open-in-disposable: hostile class (pdf) refused by the min_tier gate, no container minted" {
    vm_run "bash $PROBE hostile-class-refused"
    assert_success
    assert_output_contains "PASS: hostile class 'pdf' refused by the min_tier gate"
    assert_output_contains "PASS: no container minted for the hostile class (fail-closed)"
    assert_output_contains "PASS: hostile-class-refused"
}

@test "open-in-disposable: spawn-allowed but open-unruled -> refused at the open gate (fail-closed), no container" {
    vm_run "bash $PROBE open-gate-fail-closed"
    assert_success
    assert_output_contains "PASS: broker returns 'unknown' for the now-unruled open class"
    assert_output_contains "PASS: spawn-allowed but open-unruled -> refused at the open gate (decision=unknown)"
    assert_output_contains "PASS: no container minted on the open-gate deny path (fail-closed)"
    assert_output_contains "PASS: open-gate-fail-closed"
}
