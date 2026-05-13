# Shared helpers for VM-gated bats tests. Source from test files with
#     load helpers
# (bats-core resolves helpers.bash relative to the .bats file).

# Absolute path to vm-exec. Prefer an override via VM_EXEC env,
# otherwise derive from the repo root.
: "${VM_NAME:?set VM_NAME to the qdwin VM name}"
if [[ -z "${VM_EXEC:-}" ]]; then
    _repo_root=$(git -C "$(dirname "${BATS_TEST_FILENAME}")" \
                     rev-parse --show-toplevel 2>/dev/null)
    VM_EXEC="${_repo_root}/scripts/vm/vm-exec"
fi

# SSH transport — used by enforcing-mode VMs where qemu-guest-agent is
# denied (virt_qemu_ga_t is too restricted under SELinux=enforcing).
# When VM_SSH_PORT is set, vm_run() routes through ssh on
# 127.0.0.1:$VM_SSH_PORT instead of qga. Optional knobs:
#   VM_SSH_USER       — default 'root'
#   VM_SSH_KEY        — default ~/.ssh/qdistro_enforcing_id_ed25519
#   VM_SSH_HOST       — default 127.0.0.1
#
# virt_qemu_ga_t is too
# restricted under enforcing".
: "${VM_SSH_USER:=root}"
: "${VM_SSH_KEY:=$HOME/.ssh/qdistro_enforcing_id_ed25519}"
: "${VM_SSH_HOST:=127.0.0.1}"

# vm_run <cmd> — exec a single-line command inside the VM and capture
# stdout+stderr into $output, exit status into $status. Routes via SSH
# if VM_SSH_PORT is set, otherwise via qemu-guest-agent.
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
            "$1"
    else
        run "$VM_EXEC" "$VM_NAME" "$1"
    fi
}

# assert_success — bats-assert-like tiny shim (don't want the dep).
assert_success() {
    if [[ "$status" -ne 0 ]]; then
        echo "--- command failed (exit=$status) ---" >&2
        echo "$output" >&2
        return 1
    fi
}

# assert_output_contains <substr> — grep-style check.
assert_output_contains() {
    local needle=$1
    if ! grep -qF -- "$needle" <<<"$output"; then
        echo "--- expected substring '$needle' in output ---" >&2
        echo "$output" >&2
        return 1
    fi
}
