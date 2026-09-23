# Shared helpers for qdbrowser bats VM tests. Mirrors qdistro's pattern.
# Source in .bats files with:
#     load helpers

: "${VM_NAME:?set VM_NAME to a libvirt domain (cloned from QDWIN_VM_TEMPLATE)}"

if [[ -z "${VM_EXEC:-}" ]]; then
    # Monorepo: qdbrowser is in-tree, so the git toplevel (or, outside git,
    # four levels up from qdbrowser/tests/integration/vm/) is the qdistro
    # monorepo root, which ships scripts/vm/vm-exec. (Before the migration
    # this fell back to a sibling ../qdistro checkout.)
    _repo_root=$(git -C "$(dirname "${BATS_TEST_FILENAME}")" \
                     rev-parse --show-toplevel 2>/dev/null \
                     || cd "$(dirname "${BATS_TEST_FILENAME}")/../../../.." && pwd)
    VM_EXEC="${_repo_root}/scripts/vm/vm-exec"
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
