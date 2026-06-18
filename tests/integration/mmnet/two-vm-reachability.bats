#!/usr/bin/env bats
#
# Multi-machine VM smoke test: two cloned VMs reach each other over the isolated
# udp-over-loopback L2 segment. See todo/decisions/vm-multimachine-test-infra.md.
#
# OPT-IN. This file lives under tests/integration/mmnet/ (NOT
# tests/integration/vm/) precisely so gate_bats's `tests/integration/vm/*.bats`
# glob never discovers it — the default `qci bats` / `qci full` lanes must not
# boot a second VM or create the segment. The canonical entry point is the
# qci gate:
#
#     qci mmnet            # load-aware; allocates a locked segment; reaps VMs
#
# This bats file is a thin, equivalent runner for local iteration. It is a no-op
# (single skip-style guard) unless QD_MMNET_RUN=1, so a stray
# `bats tests/integration/mmnet/` cannot accidentally spin two VMs. With
# QD_MMNET_RUN=1 it drives the SAME scripts the gate does (mmnet-alloc.sh +
# clone-mmnet.sh) and asserts the same data-plane reachability, then reaps both
# clones in teardown — even on failure.
#
# Reachability mechanism (documented per the task's B1 note):
#   - The two clones come from baseweed-baked, whose qemu-guest-agent DOES
#     connect (SELinux permissive). qga is the CONTROL plane: we use it only to
#     bring the mmnet NIC up with a static /24 and to launch the ping/TCP probes.
#   - The actual inter-VM BYTES traverse the second NIC on the udp segment, not
#     qga. So data-plane reachability does not depend on qga; qga is orchestration
#     (the same role ssh would play). This sidesteps the B1 blocker: even if qga
#     were flaky, the segment itself is a real, independent L2 path.

REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
VM="$REPO_ROOT/scripts/vm"

setup_file() {
    export MMNET_SEED=""
    export MMNET_VM_A=""
    export MMNET_VM_B=""
}

# Reap exactly the VMs this file created (tracked by name), release the seed.
_reap() {
    local statefile="${BATS_FILE_TMPDIR:-/tmp}/mmnet-created"
    [ -f "$statefile" ] || return 0
    local seed="" v
    while IFS='=' read -r k val; do
        case "$k" in
            seed) seed="$val" ;;
            vm) v="$val"
                [ -n "$v" ] || continue
                virsh -c qemu:///session destroy "$v" >/dev/null 2>&1 || true
                virsh -c qemu:///session undefine "$v" --nvram >/dev/null 2>&1 \
                    || virsh -c qemu:///session undefine "$v" >/dev/null 2>&1 || true
                rm -f "${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}/$v.qcow2" 2>/dev/null || true
                ;;
        esac
    done < "$statefile"
    [ -n "$seed" ] && "$VM/mmnet-alloc.sh" release "$seed" 2>/dev/null || true
    rm -f "$statefile"
}

teardown_file() {
    [ "${QD_MMNET_KEEP:-0}" = 1 ] && return 0
    _reap
}

@test "mmnet: two VMs reach each other over the isolated udp-loopback segment" {
    if [ "${QD_MMNET_RUN:-0}" != 1 ]; then
        skip "opt-in: run via 'qci mmnet', or set QD_MMNET_RUN=1 to drive standalone"
    fi
    # shellcheck source=scripts/vm/mmnet-config.sh
    . "$VM/mmnet-config.sh"

    local statefile="${BATS_FILE_TMPDIR:-/tmp}/mmnet-created"
    : > "$statefile"

    # 1. Allocate a unique locked segment seed.
    local seed; seed=$(MMNET_OWNER_PID=$$ "$VM/mmnet-alloc.sh" reserve)
    [ -n "$seed" ]
    echo "seed=$seed" >> "$statefile"
    export MMNET_SEED="$seed"
    echo "# mmnet seed=$seed udp-loopback A:$(mmnet_local_port a "$seed")<->B:$(mmnet_local_port b "$seed")" >&3

    # 2. Clone two peers (staggered: clone-mmnet blocks until qga is up).
    local vm_a vm_b
    vm_a=$(MMNET_SEED="$seed" "$VM/clone-mmnet.sh" mmnet-bats-a a --from-baked | grep -E '^mmnet-bats-a-' | tail -1)
    [ -n "$vm_a" ]; echo "vm=$vm_a" >> "$statefile"
    vm_b=$(MMNET_SEED="$seed" "$VM/clone-mmnet.sh" mmnet-bats-b b --from-baked | grep -E '^mmnet-bats-b-' | tail -1)
    [ -n "$vm_b" ]; echo "vm=$vm_b" >> "$statefile"

    local ip_a ip_b mac_a mac_b probe
    ip_a=$(mmnet_ip a); ip_b=$(mmnet_ip b)
    mac_a=$(mmnet_mac a "$seed"); mac_b=$(mmnet_mac b "$seed")
    probe="$MMNET_PROBE_PORT"

    # 3. Bring up each guest's mmnet NIC over qga (control plane).
    run "$VM/vm-exec" "$vm_a" "set -e
        dev=\$(ip -o link | awk -v m='$mac_a' 'tolower(\$0) ~ tolower(m){print \$2}' | sed 's/://;s/@.*//' | head -1)
        [ -n \"\$dev\" ] || { echo NO-NIC; ip -o link; exit 1; }
        ip addr add $ip_a/$MMNET_PREFIXLEN dev \"\$dev\"; ip link set \"\$dev\" up; echo dev=\$dev"
    [ "$status" -eq 0 ]
    local dev_a; dev_a=$(sed -n 's/^dev=//p' <<<"$output" | tail -1)
    [ -n "$dev_a" ]

    run "$VM/vm-exec" "$vm_b" "set -e
        dev=\$(ip -o link | awk -v m='$mac_b' 'tolower(\$0) ~ tolower(m){print \$2}' | sed 's/://;s/@.*//' | head -1)
        [ -n \"\$dev\" ] || { echo NO-NIC; ip -o link; exit 1; }
        ip addr add $ip_b/$MMNET_PREFIXLEN dev \"\$dev\"; ip link set \"\$dev\" up; echo dev=\$dev"
    [ "$status" -eq 0 ]

    # 4. Prove the route to peer B leaves via the mmnet NIC (not some other route).
    run "$VM/vm-exec" "$vm_a" "ip -o route get $ip_b | sed -n 's/.*dev \\([^ ]*\\).*/\\1/p' | head -1"
    [ "$status" -eq 0 ]
    local route_dev; route_dev=$(tr -d '[:space:]' <<<"$output")
    [ "$route_dev" = "$dev_a" ]

    # 5. ICMP A -> B.
    run "$VM/vm-exec" "$vm_a" "ping -c 3 -W 2 $ip_b"
    [ "$status" -eq 0 ]

    # 6. TCP A -> B (token round-trip). Listener prefers nc/ncat, falls back to
    #    a portable python3 one-shot server (python3 is always in baseweed).
    local token="mmnet-ok-$seed"
    local py_listener="import socket
s=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
s.bind(('0.0.0.0',$probe)); s.listen(1); s.settimeout(30)
c,_=s.accept(); c.sendall(b'$token'); c.close()"
    "$VM/vm-exec" "$vm_b" "nohup sh -c '
        if command -v nc >/dev/null 2>&1; then printf %s \"$token\" | timeout 30 nc -l -p $probe 2>/dev/null;
        elif command -v ncat >/dev/null 2>&1; then printf %s \"$token\" | timeout 30 ncat -l $probe 2>/dev/null;
        else python3 -c \"$py_listener\" 2>/dev/null; fi' >/dev/null 2>&1 & echo started" >/dev/null 2>&1 || true
    sleep 2
    run "$VM/vm-exec" "$vm_a" "
        for i in 1 2 3 4 5; do
            r=\$( (timeout 5 nc $ip_b $probe 2>/dev/null) \
                || (timeout 5 ncat $ip_b $probe 2>/dev/null) \
                || (exec 3<>/dev/tcp/$ip_b/$probe && head -c 64 <&3) 2>/dev/null )
            [ -n \"\$r\" ] && { echo \"\$r\"; exit 0; }
            sleep 1
        done
        echo NO-TCP; exit 1"
    [ "$status" -eq 0 ]
    [[ "$output" == *"$token"* ]]
}
