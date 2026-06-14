#!/bin/bash
# s07 — net-VM 3-silo topology / control-plane isolation probe (Probe 7).
#
# SCOPE: control-plane / compiled-topology correctness ONLY.
#   - Pushes a 3-silo egress policy (siloA=direct, siloB=direct, siloC=none/dark)
#     via the host-side qdistro_netvm_client against a live OpenWrt VM.
#   - Asserts the GENERATED in-VM firewall/network ruleset has the right SHAPE:
#       * per-silo zones, per-silo resolvers, idempotent re-apply
#       * NO cross-silo forwarding (no config forwarding src=siloX dest=siloY)
#       * dark silo kill-switch (siloC has zero forwardings)
#       * each silo zone references only its own device (no device bleed)
#       * fw4/nft forward chains materialized in the kernel ruleset
#   - Does NOT prove live L2 data-plane isolation (two silos actually failing to
#     exchange packets) — that requires a root-owned bridge and is deferred.
#
# Usage (env-overridable):
#   NETVM_BASE   path to the qdistro-netvm-base.qcow2 image
#   NETVM_PORT   host port for uhttpd /ubus (default 8093)
#   NETVM_RUNDIR rundir for the VM session (default /tmp/netvm-p7)
#   NETVM_NAME   VM name (default p7)
#
set -uo pipefail

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
LIB="$REPO/tests/integration/vm/lib"
SESSION_PY="$LIB/netvm_session.py"

NETVM_BASE="${NETVM_BASE:-$HOME/.local/share/libvirt/images/qdistro-netvm-base.qcow2}"
NETVM_PORT="${NETVM_PORT:-8093}"
NETVM_RUNDIR="${NETVM_RUNDIR:-/tmp/netvm-p7}"
NETVM_NAME="${NETVM_NAME:-p7}"
NETVM_USER="${NETVM_USER:-qdistro-admin}"

# All PASS/FAIL accounting happens in the Python assertion block below; this
# wrapper only boots the VM, runs that block, and maps its exit code.

# ---- cleanup trap -----------------------------------------------------------
cleanup() {
    echo "--- teardown ---"
    python3 "$SESSION_PY" down --rundir "$NETVM_RUNDIR" --purge 2>/dev/null || true
    echo "teardown complete"
}
trap cleanup EXIT

# ---- boot VM (harness provisions the qdistro-admin rpcd login) --------------
echo "=== s07: booting VM $NETVM_NAME (port $NETVM_PORT, rundir $NETVM_RUNDIR) ==="
INFO="$(python3 "$SESSION_PY" up \
    --name "$NETVM_NAME" \
    --base "$NETVM_BASE" \
    --http-port "$NETVM_PORT" \
    --rundir "$NETVM_RUNDIR")"
echo "boot info: $INFO"

URL="$(echo "$INFO"  | python3 -c 'import json,sys; print(json.load(sys.stdin)["url"])')"
PW="$(echo "$INFO"   | python3 -c 'import json,sys; print(json.load(sys.stdin)["password"])')"
echo "endpoint: $URL"

# ---- push policy + run UCI/nft assertions via the shared harness Console ----
echo "=== running UCI/nft assertions ==="
PYTHONPATH="$REPO/session_manager:$LIB" python3 - \
    "$NETVM_RUNDIR" "$URL" "$PW" "$NETVM_USER" <<'PYALL'
import sys, time, re
import json
from netvm_session import Console

rundir, url, pw, user = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
info = json.load(open(f"{rundir}/session.json"))
sock_path = info["console_sock"]

# ---- counters ---------------------------------------------------------------
checks_pass = 0
checks_fail = 0

def chk(name, got, want):
    global checks_pass, checks_fail
    if str(got) == str(want):
        print(f"PASS: {name}")
        checks_pass += 1
    else:
        print(f"FAIL: {name} (got={got!r} want={want!r})")
        checks_fail += 1

def ok(name):
    global checks_pass
    print(f"PASS: {name}")
    checks_pass += 1

def bad(name, detail=""):
    global checks_fail
    print(f"FAIL: {name}" + (f" ({detail})" if detail else ""))
    checks_fail += 1

# ---- open console + settle --------------------------------------------------
con = Console(sock_path)
rc, out = con.run("echo settled")
if rc != 0 or out.strip() != "settled":
    print(f"FAIL: console not settled rc={rc} out={out!r}", flush=True)
    sys.exit(1)
print("PASS: console settled")

# ---- push 3-silo egress policy via rpcd client ------------------------------
from qdistro_silo_egress import EgressPolicy
from qdistro_netvm_uci import SiloNet, compile_all
from qdistro_netvm_client import NetVMClient

c = NetVMClient(base_url=url, password=pw, username=user, timeout=15)
siloA = SiloNet(name="work",    index=3, policy=EgressPolicy.parse("direct"))
siloB = SiloNet(name="browser", index=4, policy=EgressPolicy.parse("direct"))
siloC = SiloNet(name="vault",   index=7, policy=EgressPolicy.parse("none"))
frags = compile_all([siloA, siloB, siloC])
frags["network"] = (
    "config interface 'silo3'\n\toption proto 'static'\n\toption device 'qdsilo3'\n"
    "\toption ipaddr '10.10.4.1'\n\toption netmask '255.255.255.0'\n"
    "config interface 'silo4'\n\toption proto 'static'\n\toption device 'qdsilo4'\n"
    "\toption ipaddr '10.10.5.1'\n\toption netmask '255.255.255.0'\n"
    "config interface 'silo7'\n\toption proto 'static'\n\toption device 'qdsilo7'\n"
    "\toption ipaddr '10.10.8.1'\n\toption netmask '255.255.255.0'\n"
) + frags["network"]
r1 = c.egress_reload(frags)
chk("egress_reload applies 3-silo policy", r1.get("applied"), True)
r2 = c.egress_reload(frags)
chk("egress_reload is idempotent (second apply)", r2.get("applied"), True)

# ---- A1: dump raw UCI firewall (silo sections) for all assertions -----------
# Use runs() to get ALL lines. Keep it short: command must fit serial comfortably.
_, fw_lines = con.runs("uci show firewall", timeout=30)
_, dhcp_lines = con.runs("uci show dhcp", timeout=30)

# Debug dump
print("# raw UCI firewall lines with 'silo':")
for l in fw_lines:
    if "silo" in l:
        print(f"  {l}")
print("# raw UCI dhcp lines with 'silo':")
for l in dhcp_lines:
    if "silo" in l:
        print(f"  {l}")

# A1: each silo has exactly ONE zone section
# Zone section names: firewall.@zone[N].name='siloX' (not rule.name which is 'siloX-dhcp-dns')
def count_exact(lines, pattern):
    """Count lines matching a regex pattern."""
    return sum(1 for l in lines if re.search(pattern, l))

z3 = count_exact(fw_lines, r"@zone\[\d+\]\.name='silo3'$")
z4 = count_exact(fw_lines, r"@zone\[\d+\]\.name='silo4'$")
z7 = count_exact(fw_lines, r"@zone\[\d+\]\.name='silo7'$")
print(f"# zone counts: silo3={z3} silo4={z4} silo7={z7}")
chk("siloA (index 3) has exactly one zone (no dup on re-apply)", z3, 1)
chk("siloB (index 4) has exactly one zone (no dup on re-apply)", z4, 1)
chk("siloC/dark (index 7) has exactly one zone (no dup on re-apply)", z7, 1)

# A2: per-silo dnsmasq instances
d3 = count_exact(dhcp_lines, r"@dnsmasq\[\d+\]\..*silo3_dns|^dhcp\.silo3_dns=dnsmasq$")
d4 = count_exact(dhcp_lines, r"^dhcp\.silo4_dns=dnsmasq$")
d7 = count_exact(dhcp_lines, r"^dhcp\.silo7_dns=dnsmasq$")
print(f"# dnsmasq counts: silo3={d3} silo4={d4} silo7={d7}")
chk("siloA has per-silo dnsmasq instance (silo3_dns)", d3, 1)
chk("siloB has per-silo dnsmasq instance (silo4_dns)", d4, 1)
chk("siloC has per-silo dnsmasq instance (silo7_dns)", d7, 1)

# A3: NO cross-silo forwarding
# Build forwarding table from firewall lines
fwd = {}
for line in fw_lines:
    m = re.search(r'@forwarding\[(\d+)\]\.(src|dest)=\'([^\']+)\'', line)
    if m:
        idx, key, val = m.group(1), m.group(2), m.group(3)
        fwd.setdefault(idx, {})[key] = val

print("# forwarding table:")
for idx, d in sorted(fwd.items()):
    print(f"  [{idx}]: {d}")

cross = 0
for idx, d in fwd.items():
    src = d.get("src", "")
    dst = d.get("dest", "")
    if re.match(r'^silo\d+$', src) and re.match(r'^silo\d+$', dst):
        print(f"# CROSS-SILO: {src} -> {dst}")
        cross += 1
chk("no cross-silo forwarding entries (siloA<->siloB, etc.)", cross, 0)

# A4: dark siloC kill-switch = zero forwardings for silo7
f7_count = sum(1 for idx, d in fwd.items() if d.get("src","") == "silo7")
chk("dark siloC (index 7) has zero forwardings (kill-switch)", f7_count, 0)

# A5: direct silos have exactly one forwarding, destination=wan
f3_entries = [(idx, d) for idx, d in fwd.items() if d.get("src","") == "silo3"]
f4_entries = [(idx, d) for idx, d in fwd.items() if d.get("src","") == "silo4"]
chk("siloA (direct) has exactly one forwarding entry", len(f3_entries), 1)
chk("siloB (direct) has exactly one forwarding entry", len(f4_entries), 1)
f3_dest = f3_entries[0][1].get("dest","") if f3_entries else ""
f4_dest = f4_entries[0][1].get("dest","") if f4_entries else ""
chk("siloA forwarding destination is 'wan' (not silo)", f3_dest, "wan")
chk("siloB forwarding destination is 'wan' (not silo)", f4_dest, "wan")

# A6: each silo zone references only its own device (no device bleed)
# firewall.@zone[N].network='silo3' (list value)
def zone_networks(lines, zone_name):
    """Return list of network interfaces listed in the named zone."""
    # First find the zone index by matching .name='zone_name'
    idx_match = None
    for l in lines:
        m = re.match(r'firewall\.@zone\[(\d+)\]\.name=\'([^\']+)\'', l)
        if m and m.group(2) == zone_name:
            idx_match = m.group(1)
            break
    if idx_match is None:
        return []
    nets = []
    for l in lines:
        m = re.match(r'firewall\.@zone\[' + re.escape(idx_match) + r'\]\.network=\'([^\']+)\'', l)
        if m:
            nets.append(m.group(1))
    return nets

nets3 = zone_networks(fw_lines, "silo3")
nets4 = zone_networks(fw_lines, "silo4")
nets7 = zone_networks(fw_lines, "silo7")
print(f"# zone networks: silo3={nets3} silo4={nets4} silo7={nets7}")
chk("siloA zone references only its own device (silo3)",
    nets3 == ["silo3"], True)
chk("siloB zone references only its own device (silo4)",
    nets4 == ["silo4"], True)
chk("siloC zone references only its own device (silo7)",
    nets7 == ["silo7"], True)

# A7: fw4/nft kernel ruleset has per-silo forward chains
_, nft_lines = con.runs("nft list chains", timeout=60)
print("# nft chains (all):")
for l in nft_lines:
    print(f"  {l}")

_, fw4_lines = con.runs("fw4 print", timeout=90)
print("# fw4 print silo references:")
for l in fw4_lines:
    if "silo" in l:
        print(f"  {l}")

all_nft = "\n".join(nft_lines)
all_fw4 = "\n".join(fw4_lines)

# Assert on the CHAIN NAME (forward_siloN) only — a bare "siloN" substring would
# match the zone name itself and pass even if no forward chain materialized.
chk("nft/fw4 kernel: forward_silo3 chain materialized",
    "forward_silo3" in all_nft or "forward_silo3" in all_fw4, True)
chk("nft/fw4 kernel: forward_silo4 chain materialized",
    "forward_silo4" in all_nft or "forward_silo4" in all_fw4, True)
chk("nft/fw4 kernel: forward_silo7 chain materialized (dark silo has zone)",
    "forward_silo7" in all_nft or "forward_silo7" in all_fw4, True)

# A8: chain bodies — direct silos jump to accept_to_wan; dark silo is empty
_, fwd3_lines = con.runs("nft list chain inet fw4 forward_silo3", timeout=30)
_, fwd4_lines = con.runs("nft list chain inet fw4 forward_silo4", timeout=30)
_, fwd7_lines = con.runs("nft list chain inet fw4 forward_silo7", timeout=30)
fwd3_body = "\n".join(fwd3_lines)
fwd4_body = "\n".join(fwd4_lines)
fwd7_body = "\n".join(fwd7_lines)
print(f"# forward_silo3 chain:\n{fwd3_body}")
print(f"# forward_silo4 chain:\n{fwd4_body}")
print(f"# forward_silo7 chain:\n{fwd7_body}")

chk("forward_silo3 has accept_to_wan jump (direct policy reaches kernel)",
    "accept_to_wan" in fwd3_body, True)
chk("forward_silo4 has accept_to_wan jump (direct policy reaches kernel)",
    "accept_to_wan" in fwd4_body, True)
chk("forward_silo7 is empty (dark policy: no WAN, no peer)",
    "accept_to_wan" not in fwd7_body and "accept_to_vpn" not in fwd7_body, True)

print(f"\n# PASS={checks_pass} FAIL={checks_fail}")
sys.exit(0 if checks_fail == 0 else 1)
PYALL
pyall_rc=$?

if [ "$pyall_rc" -eq 0 ]; then
    echo "=== s07 topology probe: ALL ASSERTIONS PASSED ==="
    exit 0
else
    echo "=== s07 topology probe: SOME ASSERTIONS FAILED (rc=$pyall_rc) ==="
    exit 1
fi
