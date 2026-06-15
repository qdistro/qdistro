#!/usr/bin/env bash
# qci module: precondition assertions
#
# The gate modules ASSUME their dependencies are met (an initialized run dir, a
# reachable libvirt session, an existing VM, ...) and call these helpers at the
# TOP of each gate to verify that assumption fast and loudly. On a violation the
# helper records a fail row in the shared results.tsv (so the report shows WHY),
# prints a diagnostic to stderr, and RETURNS the qci exit class — it does NOT
# call `exit`, so the single-process run still finalizes through finish_run.
#
# These are invariants that hold on every healthy run, so they never fire on the
# happy path; when one does fire it has caught a real broken precondition (a
# mis-wired dispatch, a vanished VM) that the old monolith would have hit later
# with a murkier error.
# shellcheck shell=bash

# qci_fail_assert <class> <subject> <msg> -> records a fail row, warns, returns class.
qci_fail_assert() {
    local class=$1 subject=$2 msg=$3
    if [ -n "${RDIR:-}" ] && [ -f "$RDIR/results.tsv" ]; then
        record_result "${GATE:-runner}" "$subject" fail "$class" \
            "$(exit_class_name "$class")" assert "" "precondition: $msg"
    fi
    printf '[qci] precondition failed (%s): %s\n' "$subject" "$msg" >&2
    return "$class"
}

# qci_assert_run_dir -> the run engine is initialized (init_run ran). Every gate
# that records results depends on this.
qci_assert_run_dir() {
    { [ -n "${RDIR:-}" ] && [ -d "$RDIR" ] && [ -f "$RDIR/results.tsv" ]; } && return 0
    qci_fail_assert "$EXIT_RUNNER" run-dir "run dir not initialized (RDIR unset/missing)"
}

# qci_assert_repo <subject> -> the qdistro checkout is present (host gates).
# `.git` is a dir in a normal clone and a FILE in a linked worktree (both are
# used here), so accept either.
qci_assert_repo() {
    { [ -d "$QDISTRO_REPO" ] && [ -e "$QDISTRO_REPO/.git" ]; } && return 0
    qci_fail_assert "$EXIT_PREFLIGHT" "${1:-repo}" "qdistro checkout missing at $QDISTRO_REPO"
}

# qci_assert_vm_tools <subject> -> the in-guest exec helper exists. Every VM gate
# drives the guest through vm-exec regardless of whether it PROVISIONS a VM, so
# vm-exec is the one universal dependency to assert here. The provisioning
# spinners (spin-test-vm.sh / spin-test-vm-gui.sh) are needed only on the
# disposable path; acquire_vm owns that and already fails as vm_provision if a
# spinner is missing, and the --vm / replay paths never spin at all — so
# asserting a spinner here would wrongly block a valid no-provision run.
qci_assert_vm_tools() {
    [ -x "$VM_TOOLS/vm-exec" ] && return 0
    qci_fail_assert "$EXIT_PREFLIGHT" "${1:-vm-tools}" "VM exec helper missing: $VM_TOOLS/vm-exec"
}

# qci_assert_vm_exists <vm> <subject> -> a worker/replay path was handed a VM
# that actually exists. Disposable gates that acquire+validate their own VM do
# not need this; it guards the "assume a running VM" worker/explicit-VM paths.
qci_assert_vm_exists() {
    local vm=$1 subject=${2:-vm}
    [ -n "$vm" ] || qci_fail_assert "$EXIT_VM_PROVISION" "$subject" "VM name is empty" || return "$EXIT_VM_PROVISION"
    "${VIRSH[@]}" dominfo "$vm" >/dev/null 2>&1 && return 0
    qci_fail_assert "$EXIT_VM_PROVISION" "$subject" "VM does not exist: $vm"
}
