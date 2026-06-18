#!/bin/bash
# mmnet-config.sh — single source of truth for the multi-machine VM test lane's
# isolated inter-VM network.
#
# This lane stands up TWO cloned VMs that must reach each other over a private
# Ethernet segment, on a rootless `qemu:///session` host where we CANNOT:
#   - create a host bridge (`ip link add type bridge` needs CAP_NET_ADMIN / root),
#   - run system libvirtd / dnsmasq for a NAT or routed libvirt network,
#   - add a kernel multicast route for the segment.
#
# So the "separate virtual network" of the decision is realised as a QEMU
# point-to-point UDP socket segment over LOOPBACK: `<interface type='udp'>` on
# each VM, where A sends to 127.0.0.1:<portB> from local 127.0.0.1:<portA>, and
# B is the mirror. The two qemu processes' Ethernet frames are tunnelled inside
# UDP datagrams that never leave `lo`, forming a private two-node L2 segment —
# no bridge, no root, no system libvirtd, no persistent libvirt network object,
# and (because it rides loopback) confined to this host.
#
# WHY UDP-over-loopback and not mcast: an `<interface type='mcast'>` needs a
# multicast-capable egress interface that loops back; on a rootless host with no
# 239/8 route the mcast frames never reach the peer qemu (ARP fails →
# "Destination Host Unreachable" — observed). libvirt 12.4 also silently drops
# the mcast `localaddr` that would have pinned it to loopback. `type='udp'` with
# explicit 127.0.0.1 source+local addresses is verified to survive the libvirt
# round-trip and delivers over loopback without any host route. For exactly two
# VMs, point-to-point UDP is also the strictest fit (no shared multicast bus for
# an accidental third-party join).
#
# Sourced by clone-mmnet.sh and ci/lib/gates/mmnet.sh. Pure data + tiny pure
# helpers; no side effects on source.
#
# Concurrency: a sibling agent may run its own multi-machine lane on the same
# host. Two runs that picked the same UDP port pair would cross-deliver into one
# segment. So the port pair is derived per-run from a seed that the caller
# allocates under a lock (mmnet-alloc.sh / gate_mmnet); the PID default is a
# best-effort fallback for a manual single run. Override any value via the env
# vars below.

# Loopback host address both endpoints bind/send on. Keeping the UDP tunnel on
# `lo` confines the segment to this host (datagrams never hit a real NIC) and
# needs no routing. Override MMNET_LOCALADDR only for an exotic host setup.
MMNET_LOCALADDR="${MMNET_LOCALADDR:-127.0.0.1}"

mmnet_seed() {
    # A 0..65535 seed. Caller may pass an explicit seed; default is the caller
    # process's PID. The qci gate allocates a LOCKED, recorded seed
    # (mmnet-alloc.sh) so concurrent runs cannot collide; the PID default is a
    # best-effort fallback for a manual single run.
    local seed="${1:-${MMNET_SEED:-$$}}"
    printf '%s' "$(( seed % 65536 ))"
}

# Size of the base-port index space. mmnet-alloc.sh allocates a free index in
# 0..MMNET_SEED_SPACE-1 and mmnet_base_port maps it BIJECTIVELY to a base port,
# so a reserved index always yields a distinct port pair (no seed->port
# aliasing). The two files MUST agree on this value; it lives here and
# mmnet-alloc.sh reads MMNET_SEED_SPACE with the same default.
MMNET_SEED_SPACE="${MMNET_SEED_SPACE:-5000}"

# mmnet_base_port [seed] — even base port for this run's UDP pair, in
# 20000..29998. Peer A binds <base> and sends to <base+1>; peer B mirrors. The
# locked allocator (mmnet-alloc.sh) hands out a unique base-port INDEX per run,
# so concurrent qci runs never share a port pair.
mmnet_base_port() {
    if [ -n "${MMNET_BASE_PORT:-}" ]; then printf '%s' "$MMNET_BASE_PORT"; return; fi
    local s; s=$(mmnet_seed "${1:-}")
    printf '%s' "$(( 20000 + (s % MMNET_SEED_SPACE) * 2 ))"
}

# mmnet_local_port <a|b> [seed] — the UDP port THIS peer binds locally.
mmnet_local_port() {
    local base; base=$(mmnet_base_port "${2:-}")
    case "$1" in
        a|A|0) printf '%s' "$base" ;;
        b|B|1) printf '%s' "$(( base + 1 ))" ;;
        *) echo "mmnet_local_port: peer must be a or b (got '$1')" >&2; return 2 ;;
    esac
}

# mmnet_remote_port <a|b> [seed] — the peer's port THIS peer sends to (the other
# peer's local port).
mmnet_remote_port() {
    case "$1" in
        a|A|0) mmnet_local_port b "${2:-}" ;;
        b|B|1) mmnet_local_port a "${2:-}" ;;
        *) echo "mmnet_remote_port: peer must be a or b (got '$1')" >&2; return 2 ;;
    esac
}

# --- In-guest addressing (configured over the control channel after boot) ----
# A private /24 the guests never route off-segment. Peer "a" and "b" get fixed
# host addresses so the smoke test can target a known IP.
MMNET_SUBNET_CIDR="${MMNET_SUBNET_CIDR:-10.99.42.0/24}"
MMNET_PREFIXLEN="${MMNET_PREFIXLEN:-24}"
MMNET_IP_A="${MMNET_IP_A:-10.99.42.10}"
MMNET_IP_B="${MMNET_IP_B:-10.99.42.11}"
# A TCP port the smoke test opens on peer "b" to prove L4 reachability, not just
# ICMP. Stays inside the guest; not host-visible.
MMNET_PROBE_PORT="${MMNET_PROBE_PORT:-19099}"

# mmnet_ip <a|b> — peer index -> guest IP.
mmnet_ip() {
    case "$1" in
        a|A|0) printf '%s' "$MMNET_IP_A" ;;
        b|B|1) printf '%s' "$MMNET_IP_B" ;;
        *) echo "mmnet_ip: peer must be a or b (got '$1')" >&2; return 2 ;;
    esac
}

# mmnet_mac <a|b> [seed] — a stable, per-run, per-peer MAC for the mmnet NIC.
# 52:54:00 is the QEMU OUI; 0x99 marks "mmnet"; next byte is the seed-low; last
# byte is the peer index. Distinct per peer AND per run so the smoke test can
# find the right NIC by MAC even if interface naming shifts.
mmnet_mac() {
    local peer="$1" s idx
    s=$(mmnet_seed "${2:-}")
    case "$peer" in
        a|A|0) idx=10 ;;
        b|B|1) idx=11 ;;
        *) echo "mmnet_mac: peer must be a or b (got '$peer')" >&2; return 2 ;;
    esac
    printf '52:54:00:99:%02x:%02x' "$(( s % 256 ))" "$idx"
}

# mmnet_interface_xml <a|b> [seed] — emit the libvirt <interface type='udp'>
# block for one peer, on stdout. This is the ONLY place the mmnet NIC XML is
# spelled out; clone-mmnet.sh writes it to a temp file and hands it to
# clone-baseweed.sh --extra-nic-xml. The default single-machine lane never calls
# this, so its clones get exactly the template's one user-mode NIC.
#
# QEMU semantics: <source address/port> is the REMOTE endpoint this NIC sends to
# (the peer's local port); the nested <local address/port> is what this NIC
# binds. A<->B are crossed mirrors, so their datagrams meet on loopback.
mmnet_interface_xml() {
    local peer="$1" seed="${2:-}" mac lport rport
    mac=$(mmnet_mac "$peer" "$seed") || return $?
    lport=$(mmnet_local_port "$peer" "$seed") || return $?
    rport=$(mmnet_remote_port "$peer" "$seed") || return $?
    cat <<EOF
    <interface type='udp'>
      <source address='$MMNET_LOCALADDR' port='$rport'>
        <local address='$MMNET_LOCALADDR' port='$lport'/>
      </source>
      <mac address='$mac'/>
      <model type='virtio'/>
    </interface>
EOF
}
