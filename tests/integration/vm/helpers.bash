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

# Host IP as seen from inside the VM. Under SLIRP/qga this is the
# QEMU convention 10.0.2.2; under passt/SSH it's whatever the guest's
# default-route gateway happens to be (the host's outbound IP, since
# passt's shared-network mode places the guest on the host's LAN).
# Discovered lazily on first use and cached for the bats run.
_vm_host_ip_cache=""
vm_host_ip() {
    if [[ -n "$_vm_host_ip_cache" ]]; then
        printf '%s' "$_vm_host_ip_cache"
        return
    fi
    if [[ -n "${VM_SSH_PORT:-}" ]]; then
        _vm_host_ip_cache=$(ssh \
            -p "$VM_SSH_PORT" \
            -i "$VM_SSH_KEY" \
            -o StrictHostKeyChecking=no \
            -o UserKnownHostsFile=/dev/null \
            -o LogLevel=ERROR \
            -o ConnectTimeout=5 \
            -o BatchMode=yes \
            "$VM_SSH_USER@$VM_SSH_HOST" \
            "ip route | awk '/^default/ {print \$3; exit}'" 2>/dev/null)
    fi
    [[ -z "$_vm_host_ip_cache" ]] && _vm_host_ip_cache="10.0.2.2"
    printf '%s' "$_vm_host_ip_cache"
}

# vm_run <cmd> — exec a single-line command inside the VM and capture
# stdout+stderr into $output, exit status into $status. Routes via SSH
# if VM_SSH_PORT is set, otherwise via qemu-guest-agent.
#
# Under SSH transport (passt), the SLIRP-only 10.0.2.2 literal that
# many @test bodies hardcode for fetching staged drivers is rewritten
# to the discovered gateway IP. Under qga transport (SLIRP), 10.0.2.2
# stays as-is.
vm_run() {
    local cmd="$1"
    if [[ -n "${VM_SSH_PORT:-}" ]]; then
        local host_ip
        host_ip="$(vm_host_ip)"
        cmd="${cmd//10.0.2.2/$host_ip}"
        run ssh \
            -p "$VM_SSH_PORT" \
            -i "$VM_SSH_KEY" \
            -o StrictHostKeyChecking=no \
            -o UserKnownHostsFile=/dev/null \
            -o LogLevel=ERROR \
            -o ConnectTimeout=5 \
            -o BatchMode=yes \
            "$VM_SSH_USER@$VM_SSH_HOST" \
            "$cmd"
    else
        run "$VM_EXEC" "$VM_NAME" "$cmd"
    fi
}

# vm_run_admin <cmd> — run a command inside the VM as the admin user
# (uid 1000) with a real PAM session and admin's --user systemd.
# Routes through the same transport as vm_run (qga or ssh) but wraps
# the command in `runuser -l admin -c '...'`. Use this for any test
# step that needs admin's user manager (systemctl --user, qdlocker.sock,
# qdshell.sock, noctalia-shell.service, etc).
vm_run_admin() {
    local cmd="$1"
    # Escape single quotes for runuser's outer 'cmd' string.
    local escaped="${cmd//\'/\'\\\'\'}"
    vm_run "runuser -l admin -c '$escaped'"
}

# start_user_session — idempotent: ensure admin's user manager is
# running and noctalia-shell + qdlocker are active. Tests that need
# the qdshell GUI alive call this in their setup_file().
# Returns 0 on success; non-zero if /run/user/1000/wayland-1 didn't
# appear within 30s (caller should fail_loud).
_user_session_started=""
start_user_session() {
    [[ -n "$_user_session_started" ]] && return 0
    vm_run "loginctl enable-linger admin >/dev/null 2>&1 || true"
    vm_run_admin "systemctl --user start noctalia-shell.service" || true
    # Wait up to 30s for the wayland socket.
    local i
    for ((i=0; i<30; i++)); do
        run vm_run "test -S /run/user/1000/wayland-1"
        [[ "$status" -eq 0 ]] && { _user_session_started=1; break; }
        sleep 1
    done
    [[ -n "$_user_session_started" ]]
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

# require <description> — fail the test loudly when a precondition
# check (vm_run, command -v, [ -x ...]) returned non-zero. Use this
# in place of the previous `skip "<dep missing>"` pattern: missing
# deps should be loud failures, not silent skips, so bake / install
# regressions surface immediately instead of masquerading as "all
# tests pass (most skipped)" green CI.
#
# Pattern:
#   vm_run "command -v xfreerdp >/dev/null"
#   require "xfreerdp not installed on VM (need freerdp3 package)"
require() {
    if [[ "$status" -ne 0 ]]; then
        echo "--- MISSING REQUIREMENT: $1 ---" >&2
        echo "vm exit=$status, output below:" >&2
        echo "$output" >&2
        return 1
    fi
}

# fail_loud <description> — alias for `require` when the test wants
# to fail unconditionally on a control-flow branch (e.g. after the
# helper script emits "SKIP:" in its output). Same shape as the
# pre-2026-05-14 `skip "..."` calls; deps should be in the bake or
# the bake is broken — silent skips masked too many missing-dep
# regressions.
fail_loud() {
    echo "--- TEST FAILED: $* ---" >&2
    return 1
}
