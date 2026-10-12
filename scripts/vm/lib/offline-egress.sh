# shellcheck shell=bash
# offline-egress.sh — enforce QCI_OFFLINE inside test guests. Sourced.
#
# QCI_OFFLINE routes every dependency fetch to the host repo cache, but the
# routing alone does not stop a guest from reaching the internet: a fetch
# that bypasses the cache would still succeed and the run would look
# offline-clean. With no root on the host there is no per-process host
# firewall, so the block lives in the guest: a qci-owned nftables table
# that accepts loopback, private, link-local and multicast destinations
# (the host proxy is reached at the host's private IPv4 or slirp's
# 10.0.2.2) and LOGS then REJECTS everything else, on the output hook and
# on the forward hook (rootful containers, silo netns). Rejects are fast
# (tcp reset / icmp), so a blocked fetch fails at once instead of timing
# out.
#
# It is a test-harness table, never part of an image: only the spinner
# installs it (scripts/vm/spin-test-vm.sh, offline only), and the table and
# its unit are qci-named so a product nft listing can tell them apart.
#
# Gate side: qd_offline_egress_scan prints the blocked attempts logged since
# the install cursor (a worker cloned from a golden re-arms the cursor, so
# the golden's own history is not reported twice). ci/lib/vm.sh turns any
# attempt to a destination outside QCI_OFFLINE_EGRESS_EXPECTED into a failed
# row.

QD_OFFLINE_EGRESS_TABLE=qci_offline_egress
QD_OFFLINE_EGRESS_PREFIX='QCI-OFFLINE-EGRESS: '
QD_OFFLINE_EGRESS_NFT=/etc/qdistro/qci-offline-egress.nft
QD_OFFLINE_EGRESS_UNIT=qci-offline-egress.service
QD_OFFLINE_EGRESS_CURSOR=/var/lib/qci-offline-egress/cursor

# Product runtime chatter that is EXPECTED to be blocked offline and is not a
# dependency fetch (it still is blocked; the gate only does not fail on it).
# Space-separated tokens, each optionally suffixed @<uid> to match only
# packets that uid sent (the log carries UID=):
#   <host>         its addresses, resolved on the host and in the guest at
#                  scan time (weather/geocoding addresses rotate)
#   <proto>/<port> any destination on that port (NTP: chrony's pool)
# github.com is qdshell's plugin-registry fetch, so it is scoped to the
# session user (uid 1000): a root build fetching a meson wrap from github is
# still a failure. Keep hosts whose addresses are shared with a package or
# registry host out: NetworkManager's conncheck.opensuse.org shares
# mirrors.opensuse.org's address, so its check is switched off instead.
QCI_OFFLINE_EGRESS_EXPECTED="${QCI_OFFLINE_EGRESS_EXPECTED-api.open-meteo.com@1000 geocoding-api.open-meteo.com@1000 github.com@1000 udp/123}"

# qd_offline_egress_enabled — QCI_OFFLINE or QDISTRO_DEPS_OFFLINE truthy.
qd_offline_egress_enabled() {
    local v
    for v in "${QCI_OFFLINE:-}" "${QDISTRO_DEPS_OFFLINE:-}"; do
        case "$v" in 1|true|yes|on) return 0 ;; esac
    done
    return 1
}

# qd_offline_egress_ruleset — the guest nft ruleset (idempotent to re-apply:
# the add/delete pair drops any earlier copy of the table first).
qd_offline_egress_ruleset() {
    cat <<EOF
table inet $QD_OFFLINE_EGRESS_TABLE
delete table inet $QD_OFFLINE_EGRESS_TABLE
table inet $QD_OFFLINE_EGRESS_TABLE {
    counter blocked {
    }
    set allow4 {
        type ipv4_addr
        flags interval
        elements = { 0.0.0.0/8, 10.0.0.0/8, 127.0.0.0/8, 169.254.0.0/16,
                     172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/4,
                     255.255.255.255 }
    }
    set allow6 {
        type ipv6_addr
        flags interval
        elements = { ::1, fc00::/7, fe80::/10, fec0::/10, ff00::/8 }
    }
    chain egress {
        ip daddr @allow4 return
        ip6 daddr @allow6 return
        limit rate 10/second burst 20 packets log prefix "$QD_OFFLINE_EGRESS_PREFIX" level warn flags skuid
        counter name "blocked"
        meta l4proto tcp reject with tcp reset
        reject
    }
    chain output {
        type filter hook output priority filter - 10; policy accept;
        oifname "lo" accept
        jump egress
    }
    chain forward {
        type filter hook forward priority filter - 10; policy accept;
        jump egress
    }
}
EOF
}

# qd_offline_egress_unit — the oneshot that re-applies the table on every
# boot, before any network comes up.
qd_offline_egress_unit() {
    cat <<EOF
[Unit]
Description=qci offline mode: block guest egress outside the host cache
DefaultDependencies=no
Before=network-pre.target
Wants=network-pre.target
After=local-fs.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f $QD_OFFLINE_EGRESS_NFT

[Install]
WantedBy=sysinit.target
EOF
}

# qd_offline_egress_guest_script — the root shell script that installs the
# table, its unit and the NetworkManager conncheck drop-in, then records the
# journal cursor the scan starts from. Fails (nonzero) when nft is missing
# or the table is not live afterwards: offline enforcement fails closed.
qd_offline_egress_guest_script() {
    local rules unit
    rules="$(qd_offline_egress_ruleset | base64 -w0)"
    unit="$(qd_offline_egress_unit | base64 -w0)"
    cat <<EOF
set -eu
command -v nft >/dev/null 2>&1 || { echo "offline-egress: nft not installed in the guest" >&2; exit 3; }
mkdir -p "$(dirname "$QD_OFFLINE_EGRESS_NFT")" "$(dirname "$QD_OFFLINE_EGRESS_CURSOR")" /etc/NetworkManager/conf.d
printf '%s' '$rules' | base64 -d > "$QD_OFFLINE_EGRESS_NFT"
printf '%s' '$unit' | base64 -d > /etc/systemd/system/$QD_OFFLINE_EGRESS_UNIT
printf '[connectivity]\nenabled=false\n' > /etc/NetworkManager/conf.d/90-qci-offline-no-conncheck.conf
systemctl daemon-reload
systemctl enable $QD_OFFLINE_EGRESS_UNIT >/dev/null 2>&1
nft -f "$QD_OFFLINE_EGRESS_NFT"
nft list table inet $QD_OFFLINE_EGRESS_TABLE >/dev/null
nmcli general reload conf >/dev/null 2>&1 || true
journalctl -n0 --show-cursor --no-pager 2>/dev/null | sed -n 's/^-- cursor: //p' > "$QD_OFFLINE_EGRESS_CURSOR"
echo "offline-egress: table $QD_OFFLINE_EGRESS_TABLE live"
EOF
}

# qd_offline_egress_install <vm> [vm-exec] — install + arm in a running guest.
qd_offline_egress_install() {
    local vm="$1" vmx="${2:-${VM_TOOLS:-$(dirname "${BASH_SOURCE[0]}")/..}/vm-exec}" b64
    b64="$(qd_offline_egress_guest_script | base64 -w0)"
    "$vmx" "$vm" "printf '%s' '$b64' | base64 -d | bash"
}

# qd_offline_egress_scan <vm> [vm-exec] — print the guest's blocked-attempt
# log lines since the install cursor (empty = none), followed by one
# "QCI-OFFLINE-EGRESS-RESOLVED <host> <addr>" line per address the guest
# resolves an expected host to. rc 2 when the guest cannot be asked or the
# table is not armed: the caller must not read that as clean.
qd_offline_egress_scan() {
    local vm="$1" vmx="${2:-${VM_TOOLS:-$(dirname "${BASH_SOURCE[0]}")/..}/vm-exec}" out hosts
    hosts="$(qd_offline_egress_expected_hosts | tr '\n' ' ')"
    out="$("$vmx" "$vm" "c=\$(cat $QD_OFFLINE_EGRESS_CURSOR 2>/dev/null) || { echo QCI_EGRESS_UNARMED; exit 0; }; echo QCI_EGRESS_ARMED; journalctl -k --no-pager -o short-iso --after-cursor=\"\$c\" -g '^$QD_OFFLINE_EGRESS_PREFIX' 2>/dev/null || true; for h in $hosts; do getent ahosts \$h 2>/dev/null | awk -v h=\$h '{print \"QCI-OFFLINE-EGRESS-RESOLVED \" h \" \" \$1}' | sort -u; done")" || return 2
    case "$out" in
        QCI_EGRESS_ARMED*) printf '%s\n' "$out" | sed '1d' | grep -E "^QCI-OFFLINE-EGRESS-RESOLVED |$QD_OFFLINE_EGRESS_PREFIX" || true ;;
        *) return 2 ;;
    esac
}

# qd_offline_egress_expected_hosts — the host names in QCI_OFFLINE_EGRESS_EXPECTED.
qd_offline_egress_expected_hosts() {
    local t
    for t in $QCI_OFFLINE_EGRESS_EXPECTED; do
        t="${t%@*}"
        case "$t" in */*) ;; *) printf '%s\n' "$t" ;; esac
    done
}

# qd_offline_egress_classify — stdin: scan output. Prints "unexpected <dst>"
# and "expected <dst>" lines (one per distinct destination, addresses in
# canonical form; a destination is unexpected if ANY attempt to it is). An
# attempt is expected when it matches a QCI_OFFLINE_EGRESS_EXPECTED token: a
# host whose addresses (resolved on the host now, plus the guest's RESOLVED
# lines) include the destination or that the destination's reverse name is or
# is under (rotating CDN/load-balancer addresses), or a proto/port; and, with
# @<uid>, only when the log line carries that UID.
qd_offline_egress_classify() {
    local h resolved=""
    for h in $(qd_offline_egress_expected_hosts); do
        resolved+="$(getent ahosts "$h" 2>/dev/null | awk -v h="$h" '{print "QCI-OFFLINE-EGRESS-RESOLVED " h " " $1}' | sort -u)"$'\n'
    done
    { printf '%s' "$resolved"; cat; } | QCI_OFFLINE_EGRESS_EXPECTED="$QCI_OFFLINE_EGRESS_EXPECTED" python3 -c '
import ipaddress, os, re, subprocess, sys

def canon(a):
    try:
        return ipaddress.ip_address(a).compressed
    except ValueError:
        return a

addrs = {}      # host -> set of canonical addresses
attempts = []   # (dst, proto, port, uid)
for line in sys.stdin:
    if line.startswith("QCI-OFFLINE-EGRESS-RESOLVED "):
        _, host, addr = line.split()[:3]
        addrs.setdefault(host, set()).add(canon(addr))
        continue
    m = re.search(r" DST=(\S+) ", line)
    if not m:
        continue
    f = dict(re.findall(r"\b(PROTO|DPT|UID)=(\S+)", line))
    attempts.append((canon(m.group(1)), f.get("PROTO", "").lower(), f.get("DPT", ""), f.get("UID", "")))

rules = []
for tok in os.environ.get("QCI_OFFLINE_EGRESS_EXPECTED", "").split():
    what, _, uid = tok.partition("@")
    rules.append((what, uid))

ptr_cache = {}
def ptr(dst):
    # Reverse name, best effort (empty when it does not resolve in 3 s).
    if dst not in ptr_cache:
        try:
            out = subprocess.run(["getent", "hosts", dst], capture_output=True,
                                 text=True, timeout=3).stdout.split()
        except Exception:
            out = []
        ptr_cache[dst] = out[1].rstrip(".").lower() if len(out) > 1 else ""
    return ptr_cache[dst]

def expected(dst, proto, port, uid):
    for what, ruid in rules:
        if ruid and ruid != uid:
            continue
        if "/" in what:
            if what == proto + "/" + port:
                return True
        elif dst in addrs.get(what, ()):
            return True
        elif what in (ptr(dst), ) or ptr(dst).endswith("." + what):
            return True
    return False

verdict = {}
for a in attempts:
    ok = expected(*a)
    verdict[a[0]] = verdict.get(a[0], True) and ok
for dst in sorted(verdict):
    print(("expected " if verdict[dst] else "unexpected ") + dst)
'
}
