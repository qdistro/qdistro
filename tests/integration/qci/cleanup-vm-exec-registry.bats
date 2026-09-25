#!/usr/bin/env bats
#
# qci cleanup removes vm-exec orphan-registry directories of VMs that are no
# longer defined (blankss-pg13 r2 review, fable should-fix 3). Calls the real
# cleanup_vm_exec_registry with a fake virsh.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    TMP="$(mktemp -d)"
    BIN="$TMP/bin"; mkdir -p "$BIN"
    export QDISTRO_VM_EXEC_STATE_DIR="$TMP/reg"
    mkdir -p "$QDISTRO_VM_EXEC_STATE_DIR/live-vm" "$QDISTRO_VM_EXEC_STATE_DIR/gone-vm"
    : > "$QDISTRO_VM_EXEC_STATE_DIR/live-vm/.lock"
    echo '1 2 x -' > "$QDISTRO_VM_EXEC_STATE_DIR/gone-vm/99-1"
    cat > "$BIN/virsh" <<'V'
#!/bin/sh
[ -n "${FAKE_VIRSH_FAIL:-}" ] && exit 1
case "$*" in *"list --all --name"*) printf 'live-vm\nother\n' ;; esac
exit 0
V
    chmod +x "$BIN/virsh"
    VIRSH=("$BIN/virsh" -c qemu:///session)
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/cleanup.sh"
    LOG="$TMP/log"; : > "$LOG"
}

teardown() { rm -rf "$TMP"; }

@test "cleanup_vm_exec_registry: removes the dir of an undefined VM, keeps a defined one" {
    run cleanup_vm_exec_registry 0 "$LOG"
    [ "$status" -eq 0 ]
    [ "$output" = 1 ]
    [ ! -e "$QDISTRO_VM_EXEC_STATE_DIR/gone-vm" ]
    [ -d "$QDISTRO_VM_EXEC_STATE_DIR/live-vm" ]
    grep -q 'removed vm-exec registry .*gone-vm' "$LOG"
}

@test "cleanup_vm_exec_registry: dry-run removes nothing" {
    run cleanup_vm_exec_registry 1 "$LOG"
    [ "$output" = 1 ]
    [ -d "$QDISTRO_VM_EXEC_STATE_DIR/gone-vm" ]
    grep -q 'would remove' "$LOG"
}

@test "cleanup_vm_exec_registry: when libvirt cannot be listed, NOTHING is removed" {
    FAKE_VIRSH_FAIL=1 run cleanup_vm_exec_registry 0 "$LOG"
    [ "$output" = 0 ]
    [ -d "$QDISTRO_VM_EXEC_STATE_DIR/gone-vm" ]
    [ -d "$QDISTRO_VM_EXEC_STATE_DIR/live-vm" ]
}
