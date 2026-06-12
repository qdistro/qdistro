#!/bin/bash
# s04 — net-VM control plane end-to-end (task 4 pieces 1+3).
#
# Drives the real host-side rpcd client (qdistro_netvm_client) against a live
# OpenWrt rpcd serving the qdistro.netvm object, pushing a real compiled egress
# policy (qdistro_netvm_uci) through egress_reload and asserting it lands in the
# VM's live config + fw4. This is the VM half of the unit contract in
# tests/unit/test_netvm_client.py — both assert the same envelope, so a green
# run here means the fake faithfully modelled the real rpcd.
#
# Prereqs (the operator wires these; see net-vm/README.md):
#   QDISTRO_NETVM_UBUS_URL   ubus JSON-RPC endpoint (e.g. http://127.0.0.1:8080/ubus)
#   QDISTRO_NETVM_PASSWORD   rpcd login password
#   QDISTRO_NETVM_CONSOLE    optional: "<console.py> <sock>" to verify in-VM state
# The qdistro.netvm rpcd plugin + apply helper + baseline config must already be
# installed on the target (build-netvm-image.sh bakes them; for a probe rig,
# wget them from net-vm/image-files and snapshot baseline = current config).
set -uo pipefail

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
URL="${QDISTRO_NETVM_UBUS_URL:?set QDISTRO_NETVM_UBUS_URL}"
PW="${QDISTRO_NETVM_PASSWORD:?set QDISTRO_NETVM_PASSWORD}"

pass=0; fail=0
ok()   { echo "PASS: $1"; pass=$((pass+1)); }
bad()  { echo "FAIL: $1"; fail=$((fail+1)); }

PYTHONPATH="$REPO/session_manager" python3 - "$URL" "$PW" <<'PY'
import sys
from qdistro_silo_egress import EgressPolicy
from qdistro_netvm_uci import SiloNet, compile_all
from qdistro_netvm_client import (NetVMClient, NetVMCallError,
                                  NetVMPermissionError)
url, pw = sys.argv[1], sys.argv[2]
c = NetVMClient(base_url=url, password=pw, timeout=15)

def check(name, cond):
    print(("PASS: " if cond else "FAIL: ") + name)

# read path
check("system.board returns a release", "release" in c.board())
check("wan interface status reachable", c.interface_status("wan").get("up") is not None)

# error mapping (real uhttpd-mod-ubus returns JSON-RPC errors, not result:[rc])
try:
    c.call("does.not.exist", "frob"); check("object-not-found rejected", False)
except NetVMCallError as e:
    check("object-not-found -> NOT_FOUND(4)", e.code == 4 and not isinstance(e, NetVMPermissionError))

# session expiry -> transparent single re-login
c.logout(); c._session = "deadbeefdeadbeefdeadbeefdeadbeef"
check("stale session auto-relogins", "release" in c.board())

# egress_reload: push a real compiled policy and confirm it applies
direct = SiloNet(name="work", index=5, policy=EgressPolicy.parse("direct"))
dark = SiloNet(name="vault", index=6, policy=EgressPolicy.parse("none"))
frags = compile_all([direct, dark])
# silo L3 interfaces are created by the vif-attach step; seed them so fw4 is
# coherent (the test exercises the policy reload, not vif lifecycle).
frags["network"] = (
    "config interface 'silo5'\n\toption proto 'static'\n\toption device 'qdsilo5'\n"
    "\toption ipaddr '10.10.6.1'\n\toption netmask '255.255.255.0'\n"
    "config interface 'silo6'\n\toption proto 'static'\n\toption device 'qdsilo6'\n"
    "\toption ipaddr '10.10.7.1'\n\toption netmask '255.255.255.0'\n") + frags["network"]
r1 = c.egress_reload(frags)
check("egress_reload applies compiled policy", r1.get("applied") is True)
check("egress_reload is idempotent", c.egress_reload(frags).get("applied") is True)

# wifi_join input boundary: hostile SSID rejected before wpa_supplicant
r = c.call("qdistro.netvm", "wifi_join",
           {"device": "radio0", "ssid": "x"*40, "encryption": "none"})
check("wifi_join rejects overlong ssid", r.get("applied") is False)
PY
rc=$?

# Optional in-VM verification of the landed config. console.py prints the
# command echo + output + a __DONE__ marker, so we wrap each count in a unique
# token and grep THAT out of the noise.
if [ -n "${QDISTRO_NETVM_CONSOLE:-}" ]; then
    read -r CON SOCK <<<"$QDISTRO_NETVM_CONSOLE"
    vm_count() {  # vm_count <token> <grep-pattern-against-uci-show-firewall>
        python3 "$CON" "$SOCK" run \
          "echo $1=\$(uci show firewall | grep -cE '$2')" 10 2>/dev/null \
          | grep -oE "$1=[0-9]+" | tail -1 | cut -d= -f2
    }
    z="$(vm_count ZQ5 '@zone\[[0-9]+\]\.name=.silo5.$')"
    f="$(vm_count FW6 'forwarding.*src=.silo6.')"
    [ "$z" = 1 ] && ok "exactly one silo5 zone (no dup on re-apply)" || bad "silo5 zone count=$z"
    [ "$f" = 0 ] && ok "dark silo6 has no forwarding (kill-switch)" || bad "silo6 forwardings=$f"
fi

echo "s04 control-plane: client-side rc=$rc"
[ "$rc" -eq 0 ] && [ "$fail" -eq 0 ]
