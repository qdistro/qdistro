# Shared helpers for qdbrowser bats VM tests. Mirrors qdistro's pattern.
# Source in .bats files with:
#     load helpers

: "${VM_NAME:?set VM_NAME to a libvirt domain (cloned from QDWIN_VM_TEMPLATE)}"

if [[ -z "${VM_EXEC:-}" ]]; then
    _repo_root=$(git -C "$(dirname "${BATS_TEST_FILENAME}")" \
                     rev-parse --show-toplevel 2>/dev/null \
                     || dirname "$(dirname "$(dirname "${BATS_TEST_FILENAME}")")")
    VM_EXEC="${_repo_root}/scripts/vm/vm-exec"
    # qdbrowser ships no scripts/vm/; fall back to the sibling qdistro
    # checkout's helper (the layout spin-test-vm.sh / qci assume).
    if [[ ! -x "$VM_EXEC" && -x "${_repo_root}/../qdistro/scripts/vm/vm-exec" ]]; then
        VM_EXEC="${_repo_root}/../qdistro/scripts/vm/vm-exec"
    fi
fi

: "${VM_SSH_USER:=root}"
: "${VM_SSH_KEY:=$HOME/.ssh/qdbrowser_id_ed25519}"
: "${VM_SSH_HOST:=127.0.0.1}"

# vm_run <cmd> — exec a command inside the VM. Captures stdout+stderr
# into $output, exit status into $status. Routes via SSH when
# VM_SSH_PORT is set, otherwise via the helper's default transport
# (qemu-guest-agent).
vm_run() {
    if [[ -n "${VM_SSH_PORT:-}" ]]; then
        run ssh \
            -p "$VM_SSH_PORT" \
            -i "$VM_SSH_KEY" \
            -o StrictHostKeyChecking=no \
            -o UserKnownHostsFile=/dev/null \
            -o LogLevel=ERROR \
            -o ConnectTimeout=5 \
            -o BatchMode=yes \
            "$VM_SSH_USER@$VM_SSH_HOST" \
            "$@"
    else
        run "$VM_EXEC" "$VM_NAME" "$@"
    fi
}

# vm_journal <unit> [filters...] — pull recent journal lines for a tag.
vm_journal() {
    local tag="$1"; shift
    vm_run "journalctl --user --since '5 min ago' -t '$tag' --no-pager $*"
}
