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
# Space-separated host names, resolved on the host at scan time. Keep it to
# hosts whose addresses are not shared with a package/registry host:
# NetworkManager's conncheck.opensuse.org shares mirrors.opensuse.org's
# address, so its check is switched off in the guest instead.
QCI_OFFLINE_EGRESS_EXPECTED="${QCI_OFFLINE_EGRESS_EXPECTED-api.open-meteo.com geocoding-api.open-meteo.com}"

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
        limit rate 10/second burst 20 packets log prefix "$QD_OFFLINE_EGRESS_PREFIX" level warn
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
# log lines since the install cursor (empty = none). rc 2 when the guest
# cannot be asked or the table is not armed: the caller must not read that
# as clean.
qd_offline_egress_scan() {
    local vm="$1" vmx="${2:-${VM_TOOLS:-$(dirname "${BASH_SOURCE[0]}")/..}/vm-exec}" out
    out="$("$vmx" "$vm" "c=\$(cat $QD_OFFLINE_EGRESS_CURSOR 2>/dev/null) || { echo QCI_EGRESS_UNARMED; exit 0; }; echo QCI_EGRESS_ARMED; journalctl -k --no-pager -o short-iso --after-cursor=\"\$c\" -g '^$QD_OFFLINE_EGRESS_PREFIX' 2>/dev/null || true")" || return 2
    case "$out" in
        QCI_EGRESS_ARMED*) printf '%s\n' "$out" | sed '1d' | grep -F "$QD_OFFLINE_EGRESS_PREFIX" || true ;;
        *) return 2 ;;
    esac
}

# qd_offline_egress_classify — stdin: scan lines. Prints "unexpected <dst>"
# and "expected <dst>" lines (one per distinct destination). A destination is
# expected only when it is an address of a QCI_OFFLINE_EGRESS_EXPECTED host
# (resolved on the host now).
qd_offline_egress_classify() {
    local h ip expected=" " dst
    for h in $QCI_OFFLINE_EGRESS_EXPECTED; do
        for ip in $(getent ahosts "$h" 2>/dev/null | awk '{print $1}' | sort -u); do
            expected+="$ip "
        done
    done
    sed -n 's/.* DST=\([^ ]*\) .*/\1/p' | sort -u | while read -r dst; do
        case "$expected" in
            *" $dst "*) echo "expected $dst" ;;
            *) echo "unexpected $dst" ;;
        esac
    done
}
