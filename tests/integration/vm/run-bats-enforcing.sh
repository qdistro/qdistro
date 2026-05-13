#!/bin/bash
# run-bats-enforcing.sh — turn the phase7 enforcing-mode bats tests
# from SKIP into hard PASS by spinning up a config-pinned-enforcing VM
# and routing helpers.bash:vm_run through SSH.
#
# Workflow:
#   1. Ensure baseweed-enforcing-baked.qcow2 exists
#      (build-enforcing-baseweed.sh produces it; one-time ~10 min).
#   2. Clone --from-enforcing-baked: qcow2 chain enforcing-baked → baked
#      → baseweed, plus a unique <portForward> on the user-mode NIC and
#      SSH key already injected.
#   3. Wait for sshd. virt_qemu_ga_t is denied under enforcing so qga
#      is unusable; vm_run routes via SSH on the assigned port.
#   4. Run `bats --filter enforcing tiered-isolation.bats` with VM_NAME +
#      VM_SSH_PORT exported.
#   5. On exit, leave the VM defined for inspection unless --cleanup
#      was passed (matching the parallel runner's default behaviour).
#
# Usage:
#   ./run-bats-enforcing.sh                # run, leave VM defined
#   ./run-bats-enforcing.sh --cleanup      # destroy + undefine on exit
#   ./run-bats-enforcing.sh --vm <name>    # reuse an existing enforcing VM (skips clone)
#                                          # (caller must have already exported VM_SSH_PORT)
set -eo pipefail

CLEANUP=0
REUSE_VM=""
while [ $# -gt 0 ]; do
    case "$1" in
        --cleanup) CLEANUP=1; shift ;;
        --vm)      REUSE_VM="$2"; shift 2 ;;
        -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
        *) echo "ERROR: unknown arg '$1'" >&2; exit 2 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSITOR_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SPIKE="$COMPOSITOR_DIR/spike-6.5"
IMG="${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}"
ENFORCING="$IMG/baseweed-enforcing-baked.qcow2"

if [ -n "$REUSE_VM" ]; then
    if [ -z "${VM_SSH_PORT:-}" ]; then
        echo "ERROR: --vm <name> requires VM_SSH_PORT to be exported" >&2
        exit 2
    fi
    VM_NAME="$REUSE_VM"
    SSH_PORT="$VM_SSH_PORT"
    echo "[run-enforcing] reusing VM=$VM_NAME ssh_port=$SSH_PORT"
else
    if [ ! -f "$ENFORCING" ]; then
        echo "ERROR: $ENFORCING absent — build it via:" >&2
        echo "    $SPIKE/build-enforcing-baseweed.sh" >&2
        exit 1
    fi
    echo "[run-enforcing] cloning enforcing baked..."
    out=$("$SPIKE/clone-baseweed.sh" "phase7-enforcing" --from-enforcing-baked)
    VM_NAME=$(printf '%s\n' "$out" | sed -n '1p')
    SSH_PORT=$(printf '%s\n' "$out" | sed -n '2p' | sed 's/^ssh_port=//')
    if [ -z "$VM_NAME" ] || [ -z "$SSH_PORT" ]; then
        echo "ERROR: clone-baseweed.sh did not return both VM name + ssh_port" >&2
        echo "       got: $out" >&2
        exit 1
    fi
    echo "[run-enforcing] VM=$VM_NAME ssh_port=$SSH_PORT"
fi

cleanup() {
    rc=$?
    if [ "$CLEANUP" = 1 ] && [ -z "$REUSE_VM" ]; then
        echo "[run-enforcing] --cleanup: destroying $VM_NAME..."
        virsh -c qemu:///session destroy "$VM_NAME" >/dev/null 2>&1 || true
        virsh -c qemu:///session undefine "$VM_NAME" --nvram >/dev/null 2>&1 \
            || virsh -c qemu:///session undefine "$VM_NAME" >/dev/null 2>&1 \
            || true
        rm -f "$IMG/${VM_NAME}.qcow2"
    fi
    return $rc
}
trap cleanup EXIT

# Sanity: a single SSH round-trip before letting bats blow through 50+
# vm_run calls and trip on a misconfigured key/port. Strip CR from the
# stream — sshd-with-pty path can emit OK\r\n which fails an anchored
# `^OK$` grep even though the line is correct.
if ! ssh -p "$SSH_PORT" \
        -i "$HOME/.ssh/qdistro_enforcing_id_ed25519" \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR \
        -o ConnectTimeout=8 \
        -o BatchMode=yes \
        root@127.0.0.1 \
        'echo OK; getenforce; cat /etc/selinux/config | grep ^SELINUX=' \
        2>&1 | tr -d '\r' | tee /tmp/run-enforcing-sanity.log | grep -qE "^OK$"; then
    echo "ERROR: SSH sanity probe failed; see /tmp/run-enforcing-sanity.log" >&2
    cat /tmp/run-enforcing-sanity.log >&2
    exit 3
fi
echo "[run-enforcing] SSH sanity OK; running phase7 enforcing bats..."

# tiered-isolation.bats setup() expects pipewire as admin to be running. Cloned
# enforcing-baked VMs come up with the pipewire SOCKET present
# (linger spawned user@1000, systemd-tmpfiles created the runtime
# dir) but the daemon itself isn't auto-started — so kick it off
# once. This is also the path fresh-vm-bootstrap.sh takes for
# permissive clones (pw-setup.sh).
#
# We also reload dbus-broker so the AdminBroker1.conf policy is
# fresh in dbus-broker's memory. Without this, qdistro-admin-broker
# under the qdistro_broker_t domain fails its first
# `RequestName('com.qdistro.AdminBroker1')` with
# `org.freedesktop.DBus.Error.AccessDenied: Request to own name
# refused by policy` even though the XML grants user="root" the
# `<allow own="...">`.
#
# As of task(084) the boot-time fix lives in
# qdistro-dbus-reload.service (oneshot, RemainAfterExit, ordered
# Before=qdistro-admin-broker.service). We keep the explicit reload
# here as belt-and-suspenders for legacy enforcing-baked images
# that were sealed before the oneshot landed. See spec/30
# §"dbus-broker policy-reload mystery" for the leading hypothesis.
ssh -p "$SSH_PORT" \
    -i "$HOME/.ssh/qdistro_enforcing_id_ed25519" \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o LogLevel=ERROR \
    -o ConnectTimeout=8 \
    -o BatchMode=yes \
    root@127.0.0.1 '
        # The bake script ships the broker at /usr/libexec/qdistro/ with
        # the .fc rule loaded, so the file is correctly labelled
        # qdistro_broker_exec_t out of the box (verified task(086)).
        # qdistro-dbus-reload.service fires a SIGHUP at boot before the
        # broker activates, so dbus-broker has the .conf parsed.
        # tiered-isolation.bats setup() expects pipewire as admin to be running.
        bash /root/pw-setup.sh >/dev/null 2>&1 || true'

cd "$SCRIPT_DIR"
export VM_NAME VM_SSH_PORT="$SSH_PORT"
bats --filter enforcing tiered-isolation.bats
