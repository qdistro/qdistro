#!/usr/bin/env bats
#
# Host-only test for the bounded-wait guardrails on VM provisioning and the
# per-run golden build (ci/lib/vm.sh, H1). A wedged spinner or a slow/stuck
# disk must not stall the whole run silently: `timeout` caps both the golden
# build and disposable-VM acquisition, a breach is classified as
# golden-build / vm-provision INFRA (feeds the correlated-burst detector), and
# the golden build is recorded as its OWN attempt row with start/end epochs.
#
# NO libvirt/qemu is touched: the spinner is a fake that just sleeps past a
# 1-second timeout, and log/record_* are stubbed to capture the calls. This
# proves the induced-stall -> golden-build classification without a VM.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    TMP="$(mktemp -d)"
    RDIR="$TMP/run"; mkdir -p "$RDIR/vm"
    # Fake VM tools dir with spinners that hang past the timeout.
    VM_TOOLS="$TMP/vmtools"; mkdir -p "$VM_TOOLS"
    for s in spin-test-vm.sh spin-test-vm-gui.sh; do
        cat > "$VM_TOOLS/$s" <<'EOF'
#!/usr/bin/env bash
sleep 30
echo "qci-should-never-print"
EOF
        chmod +x "$VM_TOOLS/$s"
    done
    # Minimal env the sourced functions expect.
    EXIT_VM_PROVISION=40
    EXIT_OK=0
    VIRSH=(true)
    RUN_GOLDEN_BATS=""; RUN_GOLDEN_GUI_ADMIN=""; RUN_GOLDEN_GUI_QDWIN=""
    RUN_GOLDEN_DISKS=(); GOLDEN_INFLIGHT_VMS=()
    CALLS="$TMP/calls.txt"; : > "$CALLS"

    # Stub the runner primitives so we capture their arguments without a run tree.
    log() { :; }
    kv() { :; }
    record_result() { printf 'RESULT\t%s\n' "$*" >> "$CALLS"; }
    record_attempt() { printf 'ATTEMPT\t%s\n' "$*" >> "$CALLS"; }

    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/vm.sh"
}

teardown() {
    [ -n "${TMP:-}" ] && rm -rf "$TMP"
}

@test "ensure_run_golden: a wedged build times out and classifies golden-build infra" {
    QCI_GOLDEN_BUILD_TIMEOUT_S=1 run ensure_run_golden gui-admin
    [ "$status" -eq 40 ]           # EXIT_VM_PROVISION
    # An attempt row was recorded for the golden build, classified golden-build.
    grep -q "^ATTEMPT" "$CALLS"
    grep -qi "golden-build" "$CALLS"
    grep -qi "TIMEOUT" "$CALLS"
    # And a result row explains the bounded-wait breach.
    grep -qi "exceeded 1s (bounded wait" "$CALLS"
}

@test "ensure_run_golden: golden build is always recorded as an attempt row" {
    # Even the timeout path emits the attempt row (start/end epochs captured), so
    # golden builds are visible in the ledger for p99 tuning.
    QCI_GOLDEN_BUILD_TIMEOUT_S=1 run ensure_run_golden bats
    [ "$status" -eq 40 ]
    grep -q "golden-build" "$CALLS"
}

@test "acquire_vm: a wedged spinner times out and classifies vm_provision infra" {
    QCI_VM_PROVISION_TIMEOUT_S=1 run acquire_vm bats ""
    [ "$status" -eq 40 ]
    grep -qi "vm-provision" "$CALLS"
    grep -qi "exceeded 1s (bounded wait" "$CALLS"
}
