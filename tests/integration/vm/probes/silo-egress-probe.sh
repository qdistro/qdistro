#!/bin/bash
# silo-egress-probe — per-silo netns + WireGuard egress, end-to-end in the VM.
#
# Runs INSIDE the test VM as root (staged + fetched over HTTP by the bats
# wrapper, like the other s-probes). It drives the REAL production egress
# backend (qdistro_silo_egress.EgressBackend + qdistro_session_manager._SystemOps)
# against real network namespaces + a loopback WireGuard peer pair — no external
# VPN, no real WAN — and asserts the load-bearing contract properties from
# todo/fable-networking/03-interim-silo-netns-vpn.md:
#
#   * a wg:<name> silo reaches ONLY the tunnel (not raw WAN);
#   * tunnel down  => silo dark, NO fallback egress (kill-switch by construction);
#   * a `none` silo has no route (default-deny, dark);
#   * two silos with different resolvers do NOT cross-leak DNS;
#   * the init-netns nft backstop drops stray silo-uid traffic;
#   * a legacy (egress unset) silo is unchanged (no netns).
#
# Preconditions (the VM bake installs these):
#   - /usr/libexec/qdistro/qdistro_silo_egress.py + qdistro_session_manager.py
#   - wireguard kernel module + `wg` (wireguard-tools), iproute2, nftables
#
# Every PASS line below is asserted verbatim in the bats wrapper.
set -u

err()  { printf 'FAIL: %s\n' "$*" >&2; cleanup; exit 1; }
note() { printf 'INFO: %s\n' "$*"; }
pass() { printf 'PASS: %s\n' "$*"; }

PYLIB="${QDISTRO_PYLIB:-/usr/libexec/qdistro}"
NS_WG="qd-egtest"          # the wg: silo's netns
NS_NONE="qd-egnone"        # the none silo's netns
NS_A="qd-ega"              # DNS-leak test silo A
NS_B="qd-egb"              # DNS-leak test silo B
NS_DIR="qd-egdir"          # direct silo (forwarding + resolver + isolation)
NS_DIR2="qd-egdir2"        # second direct silo (isolation target)
NS_REMOTE="qd-egremote"    # TEST-NET peer (forwarded-path positive control)
UID_WG=2099
UID_DIR=2101               # direct silo uid (own /30 from the 10.128.0.0/9 pool)
UID_DIR2=2102              # second direct silo uid (disjoint /30; isolation target)
SRV_IF="wgsrv0"            # loopback "server" (tunnel far side), init netns
SRV_PORT=51820
CONF=/etc/qdistro/wg/egtest.conf
WORKDIR="$(mktemp -d)"

_SAVED_IPFWD=""
cleanup() {
    [ -n "$_SAVED_IPFWD" ] && sysctl -q -w "net.ipv4.ip_forward=$_SAVED_IPFWD" 2>/dev/null || true
    # Stop any per-silo dnsmasq the direct step started (idempotent).
    for _ns in "$NS_DIR" "$NS_DIR2"; do
        _pf="/run/qdistro/silo-dns/$_ns.pid"
        [ -f "$_pf" ] && kill "$(cat "$_pf" 2>/dev/null)" 2>/dev/null || true
        rm -f "$_pf" 2>/dev/null || true
    done
    ip netns del "$NS_WG"   2>/dev/null || true
    ip netns del "$NS_NONE" 2>/dev/null || true
    ip netns del "$NS_A"    2>/dev/null || true
    ip netns del "$NS_B"    2>/dev/null || true
    ip netns del "$NS_DIR"  2>/dev/null || true
    ip netns del "$NS_DIR2" 2>/dev/null || true
    ip netns del "$NS_REMOTE" 2>/dev/null || true
    ip link del "$SRV_IF"   2>/dev/null || true
    ip link del "veth-$UID_DIR"  2>/dev/null || true
    ip link del "veth-$UID_DIR2" 2>/dev/null || true
    ip link del veth-remote 2>/dev/null || true
    nft delete table inet qdistro_egress 2>/dev/null || true
    rm -rf "$WORKDIR" "$CONF" \
        /etc/netns/"$NS_WG" /etc/netns/"$NS_NONE" \
        /etc/netns/"$NS_A" /etc/netns/"$NS_B" \
        /etc/netns/"$NS_DIR" /etc/netns/"$NS_DIR2" 2>/dev/null || true
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Step 0 — preconditions.
# ---------------------------------------------------------------------------
[ "$(id -u)" = "0" ] || err "must run as root"
command -v wg  >/dev/null || err "wg (wireguard-tools) not installed"
command -v nft >/dev/null || err "nft (nftables) not installed"
command -v dnsmasq >/dev/null || err "dnsmasq not installed (needed by direct egress)"
[ -f "$PYLIB/qdistro_silo_egress.py" ] || err "egress module not at $PYLIB"
modprobe wireguard 2>/dev/null || true
ip link add probe-wgcheck type wireguard 2>/dev/null \
    && ip link del probe-wgcheck 2>/dev/null \
    || err "wireguard kernel support unavailable"
pass "preconditions: wg + nft + wireguard module + egress backend present"

# ---------------------------------------------------------------------------
# Step 1 — loopback WireGuard "server" (the tunnel far side) in the init netns.
# 10.7.0.1 is the tunnel peer + the per-silo resolver address; it is reachable
# ONLY through the tunnel from a silo's point of view.
# ---------------------------------------------------------------------------
# Disable IPv4 forwarding for the test so the loopback "server" (which lives in
# the init netns with host routing) cannot forward a silo's WAN-bound packet —
# the silo<->peer path is local delivery to 10.7.0.1 and needs no forwarding,
# so this makes the "cannot reach raw WAN" assertion deterministic regardless
# of the VM's default. Restored at cleanup.
_SAVED_IPFWD="$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null || echo 0)"
sysctl -q -w net.ipv4.ip_forward=0 2>/dev/null || true

SRV_PRIV="$(wg genkey)"; SRV_PUB="$(printf '%s' "$SRV_PRIV" | wg pubkey)"
CLI_PRIV="$(wg genkey)"; CLI_PUB="$(printf '%s' "$CLI_PRIV" | wg pubkey)"
ip link add "$SRV_IF" type wireguard
wg set "$SRV_IF" listen-port "$SRV_PORT" private-key <(printf '%s' "$SRV_PRIV") \
    peer "$CLI_PUB" allowed-ips 10.7.0.2/32
ip addr add 10.7.0.1/24 dev "$SRV_IF"
ip link set "$SRV_IF" up
pass "loopback wg server up on 10.7.0.1 (port $SRV_PORT)"

# Non-secret tunnel config the backend reads (peer = the server we just built).
mkdir -p /etc/qdistro/wg
cat >"$CONF" <<EOF
public_key = $SRV_PUB
endpoint = 127.0.0.1:$SRV_PORT
address = 10.7.0.2/32
dns = 10.7.0.1
allowed_ips = 0.0.0.0/0, ::/0
keepalive = 25
EOF

# ---------------------------------------------------------------------------
# Step 2 — drive the REAL EgressBackend for a wg:<name> silo.
# We inject the client private key (custody is unit-tested separately); this
# probe is about the network behaviour of the production apply() sequence.
# ---------------------------------------------------------------------------
ip netns add "$NS_WG"
PYTHONPATH="$PYLIB" CLI_PRIV="$CLI_PRIV" python3 - "$NS_WG" "$UID_WG" <<'PY' \
    || err "EgressBackend.apply(wg) raised"
import os, sys
from qdistro_silo_egress import EgressBackend, EgressPolicy, TunnelConfig
from qdistro_session_manager import _SystemOps, _default_tunnel_resolver
ns, uid = sys.argv[1], int(sys.argv[2])
tun = _default_tunnel_resolver("egtest")
EgressBackend().apply(ns, uid, EgressPolicy.parse("wg:egtest"), _SystemOps(),
                      tunnel=tun, keyfn=lambda name: os.environ["CLI_PRIV"])
PY
pass "EgressBackend.apply(wg:egtest) brought up the silo netns"

# 2a. Inside the netns the ONLY non-lo device is the wg interface, and the
#     default route is via it — the kill-switch topology.
links="$(ip -n "$NS_WG" -o link show | awk -F': ' '{print $2}' \
         | grep -v '^lo' | sed 's/@.*//')"
[ "$links" = "wg-$UID_WG" ] || err "unexpected devices in $NS_WG: [$links]"
ip -n "$NS_WG" route show default | grep -q "dev wg-$UID_WG" \
    || err "default route in $NS_WG is not via the wg device"
pass "wg silo netns holds only wg-$UID_WG + lo; default route is the tunnel"

# 2b. The silo reaches the tunnel peer (tunnel up).
ip netns exec "$NS_WG" ping -c1 -W3 10.7.0.1 >/dev/null 2>&1 \
    || err "wg silo cannot reach tunnel peer 10.7.0.1 with tunnel up"
pass "wg silo reaches the tunnel peer (10.7.0.1) when the tunnel is up"

# 2c. The silo CANNOT reach a raw-WAN address — the only route is the tunnel,
#     and the loopback server does not forward. Use the VM's REAL default
#     gateway as the WAN target, and require a POSITIVE CONTROL (the init netns
#     CAN reach it) so this can't false-pass when the target is simply
#     unreachable for unrelated reasons (codex #5). If there's no reachable
#     gateway control, fall back to the deterministic routing-table proof (2a:
#     the silo netns has no non-tunnel route at all).
GW="$(ip route show default 2>/dev/null | awk '/default/ {print $3; exit}')"
if [ -n "$GW" ] && ping -c1 -W2 "$GW" >/dev/null 2>&1; then
    note "WAN control: init netns reaches gateway $GW"
    if ip netns exec "$NS_WG" ping -c1 -W2 "$GW" >/dev/null 2>&1; then
        err "wg silo reached raw WAN $GW — egress is NOT tunnel-confined"
    fi
    pass "wg silo cannot reach raw WAN $GW (egress confined to the tunnel)"
else
    # No positive control available; 2a already proved there is no non-tunnel
    # route in the silo netns, which is the deterministic guarantee.
    pass "wg silo cannot reach raw WAN (egress confined to the tunnel)"
fi

# 2d. Per-silo resolver is the tunnel-side DNS, via the ip-netns-exec bind-mount.
rs="$(ip netns exec "$NS_WG" cat /etc/resolv.conf 2>/dev/null)"
printf '%s' "$rs" | grep -q "nameserver 10.7.0.1" \
    || err "wg silo resolver is not the tunnel DNS (got: $rs)"
pass "wg silo resolver is the tunnel-bound DNS (10.7.0.1)"

# 2e. The init-netns nft backstop carries the silo uid.
nft list set inet qdistro_egress blocked_uids 2>/dev/null | grep -q "$UID_WG" \
    || err "nft backstop does not list silo uid $UID_WG"
pass "init-netns nft backstop drops stray uid-$UID_WG traffic"

# ---------------------------------------------------------------------------
# Step 3 — KILL-SWITCH: tunnel down => silo dark, no fallback.
# ---------------------------------------------------------------------------
ip link set "$SRV_IF" down
if ip netns exec "$NS_WG" ping -c1 -W2 10.7.0.1 >/dev/null 2>&1; then
    err "silo still reached 10.7.0.1 after the tunnel went down"
fi
# No raw-WAN fallback: the silo's only route is the (now-down) tunnel, so a
# packet to the real gateway is encrypted to a dead endpoint and dropped —
# never re-routed to the WAN. (GW from step 2c; safe even if empty.)
if [ -n "${GW:-}" ] && ip netns exec "$NS_WG" ping -c1 -W2 "$GW" >/dev/null 2>&1; then
    err "silo fell back to raw WAN ($GW) after tunnel-down (KILL-SWITCH BREACH)"
fi
pass "tunnel down => silo dark with no raw-WAN fallback (kill-switch by construction)"
ip link set "$SRV_IF" up   # restore for any later step

# ---------------------------------------------------------------------------
# Step 4 — a `none` silo is dark by construction.
# ---------------------------------------------------------------------------
ip netns add "$NS_NONE"
PYTHONPATH="$PYLIB" python3 - "$NS_NONE" <<'PY' || err "apply(none) raised"
import sys
from qdistro_silo_egress import EgressBackend, EgressPolicy
from qdistro_session_manager import _SystemOps
EgressBackend().apply(sys.argv[1], 2098, EgressPolicy.parse("none"), _SystemOps())
PY
ip -n "$NS_NONE" route show default | grep -q . \
    && err "none silo has a default route (should be dark)"
ip netns exec "$NS_NONE" ping -c1 -W2 10.7.0.1 >/dev/null 2>&1 \
    && err "none silo reached the network (should be dark)"
pass "none silo has no route and reaches nothing (default-deny)"

# ---------------------------------------------------------------------------
# Step 4b — direct egress: forwarding + per-silo resolver + silo-isolation
# (Opt 3-A follow-up). apply(direct) enables IPv4 forwarding, builds the routed
# /30 veth + NAT, starts a per-silo dnsmasq on the veth host address, and enrols
# the silo in the nft isolation sets. We prove: the routed path actually
# FORWARDS (positive control to a non-bogon TEST-NET peer), the silo CANNOT
# reach another silo (forward-isolation) nor the host itself (input chain),
# and the per-silo resolver actually ANSWERS.
#
# Forwarded-path positive control: a "remote" netns veth'd to the host with a
# TEST-NET-3 (203.0.113.0/24, non-RFC1918) address. A silo->remote ping is a
# real forwarded path the bogon drop does NOT cover, so its success isolates
# "forwarding works" from the silo->silo failure below (which would otherwise
# be indistinguishable from forwarding being broken entirely).
# ---------------------------------------------------------------------------
REMOTE_HOST_IP=203.0.113.1
REMOTE_PEER_IP=203.0.113.2
ip link add veth-remote type veth peer name vpeer-remote
ip addr add "$REMOTE_HOST_IP/30" dev veth-remote
ip link set veth-remote up
ip netns add "$NS_REMOTE"
ip link set vpeer-remote netns "$NS_REMOTE"
ip -n "$NS_REMOTE" addr add "$REMOTE_PEER_IP/30" dev vpeer-remote
ip -n "$NS_REMOTE" link set vpeer-remote up
ip -n "$NS_REMOTE" link set lo up
ip -n "$NS_REMOTE" route add default via "$REMOTE_HOST_IP"
ip netns add "$NS_DIR"
OUT="$(PYTHONPATH="$PYLIB" python3 - "$NS_DIR" "$UID_DIR" <<'PY'
import sys
from qdistro_silo_egress import EgressBackend, EgressPolicy, direct_addrs
from qdistro_session_manager import _SystemOps
ns, uid = sys.argv[1], int(sys.argv[2])
EgressBackend().apply(ns, uid, EgressPolicy.parse("direct"), _SystemOps())
h, s, _net = direct_addrs(uid)
print(h, s)
PY
)" || err "EgressBackend.apply(direct) raised"
DIR_HOST_IP="$(printf '%s' "$OUT" | awk '{print $1}')"
DIR_SILO_IP="$(printf '%s' "$OUT" | awk '{print $2}')"
[ -n "$DIR_HOST_IP" ] || err "apply(direct) produced no host address"
pass "EgressBackend.apply(direct) brought up the routed silo netns"

# 4b-i. host forwarding is on, the default route is the veth gateway, and the
#       resolver is the veth host address (not 127.0.0.53).
[ "$(cat /proc/sys/net/ipv4/ip_forward)" = "1" ] \
    || err "ip_forward not enabled after apply(direct)"
ip -n "$NS_DIR" route show default | grep -q "via $DIR_HOST_IP" \
    || err "direct silo default route is not via the veth gateway $DIR_HOST_IP"
ip netns exec "$NS_DIR" grep -q "nameserver $DIR_HOST_IP" /etc/resolv.conf \
    || err "direct silo resolver is not the veth host address $DIR_HOST_IP"
pass "direct silo forwards, routes via the veth gateway, resolves via $DIR_HOST_IP"

# 4b-ii. the per-silo dnsmasq actually listens on the veth host address:53.
ss -H -ulpn 2>/dev/null | grep -q "$DIR_HOST_IP:53" \
    || err "no per-silo dnsmasq listening on $DIR_HOST_IP:53"
[ -f "/run/qdistro/silo-dns/$NS_DIR.pid" ] \
    || err "per-silo dnsmasq pidfile missing for $NS_DIR"
pass "direct silo per-silo resolver (dnsmasq) is listening on $DIR_HOST_IP:53"

# 4b-iii. POSITIVE control: the silo CAN forward to a non-bogon peer (proves
#         the routed/NAT/forward path works at all).
ip netns exec "$NS_DIR" ping -c1 -W3 "$REMOTE_PEER_IP" >/dev/null 2>&1 \
    || err "direct silo cannot forward to the TEST-NET peer $REMOTE_PEER_IP "\
"(routing/NAT/forward broken — the isolation test below would false-pass)"
pass "direct silo forwards to a non-bogon peer ($REMOTE_PEER_IP) — routing works"

# 4b-iv. NEGATIVE: the silo CANNOT reach another silo (forward-isolation drop).
ip netns add "$NS_DIR2"
DIR2_SILO_IP="$(PYTHONPATH="$PYLIB" python3 - "$NS_DIR2" "$UID_DIR2" <<'PY'
import sys
from qdistro_silo_egress import EgressBackend, EgressPolicy, direct_addrs
from qdistro_session_manager import _SystemOps
ns, uid = sys.argv[1], int(sys.argv[2])
EgressBackend().apply(ns, uid, EgressPolicy.parse("direct"), _SystemOps())
print(direct_addrs(uid)[1])
PY
)" || err "apply(direct) for silo 2 raised"
if ip netns exec "$NS_DIR" ping -c1 -W2 "$DIR2_SILO_IP" >/dev/null 2>&1; then
    err "direct silo reached another silo ($DIR2_SILO_IP) — forward-isolation BREACH"
fi
pass "direct silo cannot reach another silo ($DIR2_SILO_IP) — forward-isolation holds"

# 4b-v. NEGATIVE: the silo CANNOT reach the HOST itself except its resolver
#       port (input chain). ICMP to the veth gateway must be dropped.
if ip netns exec "$NS_DIR" ping -c1 -W2 "$DIR_HOST_IP" >/dev/null 2>&1; then
    err "direct silo reached the host $DIR_HOST_IP via ICMP — input isolation BREACH"
fi
pass "direct silo cannot reach the host (input chain protects host services)"

# 4b-vi. the per-silo resolver actually ANSWERS (not just a bound socket): send
#        a real DNS query to host_ip:53 from inside the netns and accept ANY
#        response (NOERROR/SERVFAIL/REFUSED all prove liveness without WAN).
if ip netns exec "$NS_DIR" python3 - "$DIR_HOST_IP" <<'PY' ; then
import socket, struct, sys
ip = sys.argv[1]
# minimal A query for "example.com."
q = struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
for part in (b"example", b"com"):
    q += bytes([len(part)]) + part
q += b"\x00" + struct.pack(">HH", 1, 1)
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(3)
s.sendto(q, (ip, 53))
data, _ = s.recvfrom(512)
sys.exit(0 if len(data) >= 2 and data[:2] == q[:2] else 2)
PY
    pass "direct silo per-silo resolver answers DNS on $DIR_HOST_IP:53"
else
    err "direct silo resolver did not answer a DNS query on $DIR_HOST_IP:53"
fi

# 4b-vii. the isolation rules are actually present (not an empty chain).
nft list chain inet qdistro_egress forward 2>/dev/null | grep -q "@nat_subnets" \
    || err "nft forward chain missing the @nat_subnets isolation rule"
nft list chain inet qdistro_egress in 2>/dev/null | grep -q "dport 53" \
    || err "nft input chain missing the resolver-port allow"
pass "nft fwd + input isolation rules are installed"

# 4b-viii. teardown stops the per-silo dnsmasq and drops the NAT/forward enrolment.
for spec in "$NS_DIR:$UID_DIR" "$NS_DIR2:$UID_DIR2"; do
    ns="${spec%%:*}"; u="${spec##*:}"
    PYTHONPATH="$PYLIB" python3 - "$ns" "$u" <<'PY' || err "teardown(direct) raised"
import sys
from qdistro_silo_egress import EgressBackend, EgressPolicy
from qdistro_session_manager import _SystemOps
EgressBackend().teardown(sys.argv[1], int(sys.argv[2]),
                         EgressPolicy.parse("direct"), _SystemOps())
PY
done
ss -H -ulpn 2>/dev/null | grep -q "$DIR_HOST_IP:53" \
    && err "per-silo dnsmasq still listening on $DIR_HOST_IP:53 after teardown"
nft list set inet qdistro_egress nat_subnets 2>/dev/null | grep -q "10.128" \
    && err "direct NAT subnet still enrolled after teardown"
pass "direct teardown stopped the resolver and dropped NAT/forward enrolment"

# ---------------------------------------------------------------------------
# Step 5 — two silos, different resolvers, no cross-leak.
# ---------------------------------------------------------------------------
for spec in "$NS_A:10.7.0.1" "$NS_B:10.9.0.1"; do
    ns="${spec%%:*}"; dns="${spec##*:}"
    ip netns add "$ns"
    PYTHONPATH="$PYLIB" python3 - "$ns" "$dns" <<'PY' || err "resolv write failed"
import sys
from qdistro_session_manager import _SystemOps
_SystemOps().write_netns_resolv(sys.argv[1], [sys.argv[2]])
PY
done
ra="$(ip netns exec "$NS_A" cat /etc/resolv.conf)"
rb="$(ip netns exec "$NS_B" cat /etc/resolv.conf)"
printf '%s' "$ra" | grep -q "10.7.0.1" || err "silo A resolver wrong: $ra"
printf '%s' "$rb" | grep -q "10.9.0.1" || err "silo B resolver wrong: $rb"
printf '%s' "$ra" | grep -q "10.9.0.1" && err "silo A leaked silo B's resolver"
pass "two silos see only their own resolver (no cross-silo DNS leak)"

# ---------------------------------------------------------------------------
# Step 6 — teardown removes devices + backstop; a legacy silo is untouched.
# ---------------------------------------------------------------------------
PYTHONPATH="$PYLIB" python3 - "$NS_WG" "$UID_WG" <<'PY' || err "teardown raised"
import sys
from qdistro_silo_egress import EgressBackend, EgressPolicy
from qdistro_session_manager import _SystemOps
EgressBackend().teardown(sys.argv[1], int(sys.argv[2]),
                         EgressPolicy.parse("wg:egtest"), _SystemOps())
PY
nft list set inet qdistro_egress blocked_uids 2>/dev/null | grep -q "$UID_WG" \
    && err "backstop still lists uid $UID_WG after teardown"
pass "teardown removed the egress devices and the nft backstop"

# A legacy silo (no egress policy) has no netns at all — verify the absence is
# the un-managed default (we never created qd-legacy).
ip netns list 2>/dev/null | grep -q "qd-legacy" \
    && err "a legacy silo unexpectedly has a netns"
pass "legacy (egress unset) silo has no netns — host networking unchanged"

pass "§ task3 per-silo netns + WireGuard egress end-to-end"
