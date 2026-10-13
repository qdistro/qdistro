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
# 10.0.2.2), plus DNS (port 53) to the host's own nameservers — passt hands
# the guest those, e.g. a tailnet 100.100.100.100 — and REJECTS everything
# else, on the output hook and on the
# forward hook (rootful containers, silo netns). Rejects are fast (tcp
# reset / icmp), so a blocked fetch fails at once instead of timing out.
# An accept in another table cannot bypass it: every base chain on a hook
# sees the packet.
#
# Every blocked attempt is RECORDED in dynamic sets inside the table
# (destination, protocol, port and, for local sockets, the sending uid),
# each element with a packet counter: lossless, unlike a rate-limited log,
# and independent of the journal. A packet whose port or uid cannot be read
# still counts against its destination, so "blocked more often than
# detailed" is visible. The log line stays for humans and is rate-limited.
#
# The table is reloaded at every boot by a sysinit unit, so a VM cloned
# from an armed golden records from its own boot. Every reload path first
# dumps the live table into $QD_OFFLINE_EGRESS_STATE/history (the unit's
# ExecStop, which also runs on restart and before a reboot, and the
# installer), and the scan merges that history: a reload or reboot never
# erases what was recorded. A golden is SEALED after its own scan (history
# cleared, ExecStop dumps suppressed until the next boot), so its clones
# start with no history of the golden's.
#
# Boot window: a VM first armed after boot could reach the internet before
# the table exists. qd_offline_egress_start_dark starts it with every NIC
# link down; qga (virtio-serial, no network) installs the table; only then
# do the links come up (qd_offline_egress_links_up).
#
# It is a test-harness table, never part of an image: only the spinner and
# the mmnet gate arm guests (via QD_OFFLINE_EGRESS_ARM=1 to
# scripts/vm/clone-baseweed.sh), offline only, and the table and its unit are
# qci-named so a product nft listing can tell them apart.
#
# Gate side: qd_offline_egress_scan dumps the live table plus a reference
# copy of the ruleset loaded into a throwaway netns in the guest;
# offline-egress-classify.py fails a table that differs from the reference
# (rules, chains, allow sets), and turns every recorded destination outside
# QCI_OFFLINE_EGRESS_EXPECTED into an "unexpected" line, which ci/lib/vm.sh
# records as a failed row.

QD_OFFLINE_EGRESS_TABLE=qci_offline_egress
QD_OFFLINE_EGRESS_PREFIX='QCI-OFFLINE-EGRESS: '
QD_OFFLINE_EGRESS_NFT=/etc/qdistro/qci-offline-egress.nft
QD_OFFLINE_EGRESS_UNIT=qci-offline-egress.service
QD_OFFLINE_EGRESS_SET_SIZE=65535
QD_OFFLINE_EGRESS_STATE=/var/lib/qci-offline-egress
QD_OFFLINE_EGRESS_LIBDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Product runtime chatter that is EXPECTED to be blocked offline and is not a
# dependency fetch (it still is blocked; the gate only does not fail on it).
# Space-separated tokens, each optionally suffixed @<uid> to match only
# packets that uid sent (forwarded packets carry no uid, so a scoped token
# never matches them):
#   <host>         its addresses, resolved on the host and in the guest at
#                  scan time, or a destination whose reverse name is the host
#                  or under it AND resolves back to the destination
#                  (weather/github load-balancer addresses rotate)
#   <proto>/<port> any destination on that port (NTP: chrony's pool)
# github.com is qdshell's plugin-registry fetch, so it is scoped to the
# session user (uid 1000): a root build fetching e.g. a meson wrap from
# github still fails. Keep hosts whose addresses are shared with a package or
# registry host out: NetworkManager's conncheck.opensuse.org shares
# mirrors.opensuse.org's address, so its check is switched off instead (here
# and in the tier-5 per-app guest image).
QCI_OFFLINE_EGRESS_EXPECTED="${QCI_OFFLINE_EGRESS_EXPECTED-api.open-meteo.com@1000 geocoding-api.open-meteo.com@1000 github.com@1000 udp/123}"

# qd_offline_egress_enabled — QCI_OFFLINE or QDISTRO_DEPS_OFFLINE truthy.
qd_offline_egress_enabled() {
    local v
    for v in "${QCI_OFFLINE:-}" "${QDISTRO_DEPS_OFFLINE:-}"; do
        case "$v" in 1|true|yes|on) return 0 ;; esac
    done
    return 1
}

# qd_offline_egress_arm_requested — offline AND the caller asked clone paths
# to arm (QD_OFFLINE_EGRESS_ARM=1). Image builders never set it, so the table
# is never sealed into a baked image.
qd_offline_egress_arm_requested() {
    [ "${QD_OFFLINE_EGRESS_ARM:-0}" = 1 ] && qd_offline_egress_enabled
}

# qd_offline_egress_dns_rules — accept rules for DNS (udp/tcp 53) to the
# host's nameservers that the allow sets do not already cover (passt gives
# the guest the host's resolv.conf servers). Only port 53, only those
# addresses: the rest of e.g. a tailnet range stays blocked and recorded.
qd_offline_egress_dns_rules() {
    local f=${QD_OFFLINE_EGRESS_RESOLV_CONF:-/etc/resolv.conf} a v4="" v6=""
    for a in $(awk '$1 == "nameserver" { print $2 }' "$f" 2>/dev/null); do
        case "$a" in
            10.*|127.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*|169.254.*) ;;
            *:*) case "$a" in ::1|fe80:*|f[cd]*) ;; *) v6+="${v6:+, }$a" ;; esac ;;
            *.*.*.*) v4+="${v4:+, }$a" ;;
        esac
    done
    [ -n "$v4" ] && printf '        ip daddr { %s } meta l4proto { udp, tcp } th dport 53 accept\n' "$v4"
    [ -n "$v6" ] && printf '        ip6 daddr { %s } meta l4proto { udp, tcp } th dport 53 accept\n' "$v6"
    return 0
}

# qd_offline_egress_ruleset — the guest nft ruleset (idempotent to re-apply:
# the add/delete pair drops any earlier copy of the table first).
qd_offline_egress_ruleset() {
    local n=$QD_OFFLINE_EGRESS_SET_SIZE dns
    dns="$(qd_offline_egress_dns_rules)"
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
    # Documentation (TEST-NET) ranges pass only out a veth, i.e. to a peer
    # namespace inside the guest (silo-egress's forwarding peer); routed
    # anywhere else they are blocked and recorded like any address.
    set doc4 {
        type ipv4_addr
        flags interval
        elements = { 192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24 }
    }
    set doc6 {
        type ipv6_addr
        flags interval
        elements = { 2001:db8::/32 }
    }
    set any4 { typeof ip daddr; size $n; flags dynamic; counter; }
    set any6 { typeof ip6 daddr; size $n; flags dynamic; counter; }
    set out4 { typeof ip daddr . meta l4proto . th dport . meta skuid; size $n; flags dynamic; counter; }
    set out6 { typeof ip6 daddr . meta l4proto . th dport . meta skuid; size $n; flags dynamic; counter; }
    set fwd4 { typeof ip daddr . meta l4proto . th dport; size $n; flags dynamic; counter; }
    set fwd6 { typeof ip6 daddr . meta l4proto . th dport; size $n; flags dynamic; counter; }
    chain block {
        limit rate 10/second burst 20 packets log prefix "$QD_OFFLINE_EGRESS_PREFIX" level warn flags skuid
        counter name "blocked"
        meta l4proto tcp reject with tcp reset
        reject
    }
    chain output {
        type filter hook output priority filter - 10; policy accept;
        oifname "lo" accept
        ip daddr @allow4 accept
        ip6 daddr @allow6 accept
        oifname "veth*" ip daddr @doc4 accept
        oifname "veth*" ip6 daddr @doc6 accept
${dns:+$dns
}        update @any4 { ip daddr }
        update @any6 { ip6 daddr }
        update @out4 { ip daddr . meta l4proto . th dport . meta skuid }
        update @out6 { ip6 daddr . meta l4proto . th dport . meta skuid }
        jump block
    }
    chain forward {
        type filter hook forward priority filter - 10; policy accept;
        ip daddr @allow4 accept
        ip6 daddr @allow6 accept
        oifname "veth*" ip daddr @doc4 accept
        oifname "veth*" ip6 daddr @doc6 accept
        update @any4 { ip daddr }
        update @any6 { ip6 daddr }
        update @fwd4 { ip daddr . meta l4proto . th dport }
        update @fwd6 { ip6 daddr . meta l4proto . th dport }
        jump block
    }
}
EOF
}

# qd_offline_egress_dump_cmd — sh that saves the live table into the
# history, named <boot_id>-<ns>.json. Only a table nft reports as absent
# ("No such file or directory") is nothing to save; any other failure — of
# the lookup or of the dump — leaves a sticky dump-failed marker (the scan
# reports it as broken) and fails the command. No single quotes: the unit
# wraps it in sh -c '...'. In a unit file, % is doubled.
qd_offline_egress_dump_cmd() {
    local h=$QD_OFFLINE_EGRESS_STATE/history t=$QD_OFFLINE_EGRESS_TABLE
    printf '%s' "if e=\$(nft list table inet $t 2>&1 >/dev/null); then mkdir -p $h && f=$h/\$(cat /proc/sys/kernel/random/boot_id)-\$(date +%s%N).json && nft -j list table inet $t > \$f.tmp && mv \$f.tmp \$f || { touch $QD_OFFLINE_EGRESS_STATE/dump-failed; false; }; else case \"\$e\" in *\"No such file or directory\"*) ;; *) touch $QD_OFFLINE_EGRESS_STATE/dump-failed; false ;; esac; fi"
}

# qd_offline_egress_unit — the oneshot that loads the table on every boot,
# before any network comes up, and dumps it to the history when stopped
# (restart, reboot, shutdown) unless the guest is a sealed golden. It must
# load AFTER openSUSE's soft-reboot-cleanup.service (aaa_base), which runs
# `nft flush ruleset` on every boot and is only ordered before
# network-pre.target: loaded first, the table was flushed a moment later.
qd_offline_egress_unit() {
    local dump
    dump="$(qd_offline_egress_dump_cmd | sed 's/%/%%/g')"
    cat <<EOF
[Unit]
Description=qci offline mode: block guest egress outside the host cache
DefaultDependencies=no
Before=network-pre.target shutdown.target
Wants=network-pre.target
Conflicts=shutdown.target
After=local-fs.target soft-reboot-cleanup.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=-/bin/rm -f $QD_OFFLINE_EGRESS_STATE/sealed
ExecStart=/usr/sbin/nft -f $QD_OFFLINE_EGRESS_NFT
ExecStop=/bin/sh -c '[ -e $QD_OFFLINE_EGRESS_STATE/sealed ] || { $dump; }'

[Install]
WantedBy=sysinit.target
EOF
}

# qd_offline_egress_guest_script — the root shell script that installs the
# table, its unit and the NetworkManager conncheck drop-in, and (re)loads the
# table — after dumping a live one into the history, so its recorded attempts
# are kept and whatever the live table was, the loaded one is the ruleset.
# Fails (nonzero) when nft is missing or the table is not live afterwards:
# offline enforcement fails closed.
qd_offline_egress_guest_script() {
    local rules unit
    rules="$(qd_offline_egress_ruleset | base64 -w0)"
    unit="$(qd_offline_egress_unit | base64 -w0)"
    cat <<EOF
set -eu
command -v nft >/dev/null 2>&1 || { echo "offline-egress: nft not installed in the guest" >&2; exit 3; }
mkdir -p "$(dirname "$QD_OFFLINE_EGRESS_NFT")" "$QD_OFFLINE_EGRESS_STATE" /etc/NetworkManager/conf.d
printf '%s' '$rules' | base64 -d > "$QD_OFFLINE_EGRESS_NFT"
printf '%s' '$unit' | base64 -d > /etc/systemd/system/$QD_OFFLINE_EGRESS_UNIT
printf '[connectivity]\nenabled=false\n' > /etc/NetworkManager/conf.d/90-qci-offline-no-conncheck.conf
systemctl daemon-reload
systemctl enable $QD_OFFLINE_EGRESS_UNIT >/dev/null 2>&1
rm -f "$QD_OFFLINE_EGRESS_STATE/sealed"
$(qd_offline_egress_dump_cmd)
nft -f "$QD_OFFLINE_EGRESS_NFT"
nft list table inet $QD_OFFLINE_EGRESS_TABLE >/dev/null
echo "offline-egress: table $QD_OFFLINE_EGRESS_TABLE live"
nmcli general reload conf >/dev/null 2>&1 || true
EOF
}

# qd_offline_egress_install <vm> [vm-exec] — install + arm in a running guest.
qd_offline_egress_install() {
    local vm="$1" vmx="${2:-${VM_TOOLS:-$QD_OFFLINE_EGRESS_LIBDIR/..}/vm-exec}" b64
    b64="$(qd_offline_egress_guest_script | base64 -w0)"
    "$vmx" "$vm" "printf '%s' '$b64' | base64 -d | bash"
}

# qd_offline_egress_macs <vm> — the MACs of the domain's interfaces. Fails
# when the listing fails or finds none: no NIC verified down is not "dark".
qd_offline_egress_macs() {
    local list macs
    list="$(virsh -c qemu:///session domiflist "$1")" || return 1
    macs="$(printf '%s\n' "$list" | awk 'NR > 2 && $NF ~ /^([0-9a-f][0-9a-f]:){5}[0-9a-f][0-9a-f]$/ { print $NF }')"
    [ -n "$macs" ] || return 1
    printf '%s\n' "$macs"
}

# qd_offline_egress_set_links <vm> up|down — set every NIC's link state and
# verify it took.
qd_offline_egress_set_links() {
    local vm="$1" state="$2" mac macs
    macs="$(qd_offline_egress_macs "$vm")" || return 1
    for mac in $macs; do
        virsh -c qemu:///session domif-setlink "$vm" "$mac" "$state" >/dev/null || return 1
        virsh -c qemu:///session domif-getlink "$vm" "$mac" 2>/dev/null | grep -qw "$state" || return 1
    done
}

# qd_offline_egress_start_dark <vm> — start a defined domain with every NIC
# link down from its first instruction (start paused, links down, resume).
qd_offline_egress_start_dark() {
    local vm="$1"
    virsh -c qemu:///session start --paused "$vm" >/dev/null || return 1
    qd_offline_egress_set_links "$vm" down || {
        echo "offline-egress: could not take $vm's links down" >&2
        virsh -c qemu:///session destroy "$vm" >/dev/null 2>&1
        return 1
    }
    virsh -c qemu:///session resume "$vm" >/dev/null
}

# qd_offline_egress_links_up <vm> [vm-exec] — after the install: links up,
# then wait (90 s) for the guest to have a default route again.
qd_offline_egress_links_up() {
    local vm="$1" vmx="${2:-${VM_TOOLS:-$QD_OFFLINE_EGRESS_LIBDIR/..}/vm-exec}"
    qd_offline_egress_set_links "$vm" up || return 1
    # shellcheck disable=SC2016  # expands in the guest
    "$vmx" "$vm" 'for i in $(seq 90); do ip route show default | grep -q . && exit 0; sleep 1; done; exit 1' >/dev/null
}

# qd_offline_egress_seal <vm> [vm-exec] — after a golden's final scan (taken
# with its links down, so nothing it does afterwards can leave it): clear its
# history and suppress the shutdown dump, so clones start without the
# golden's history. The next boot (a clone's) unseals.
qd_offline_egress_seal() {
    local vm="$1" vmx="${2:-${VM_TOOLS:-$QD_OFFLINE_EGRESS_LIBDIR/..}/vm-exec}"
    "$vmx" "$vm" "mkdir -p $QD_OFFLINE_EGRESS_STATE && touch $QD_OFFLINE_EGRESS_STATE/sealed && rm -rf $QD_OFFLINE_EGRESS_STATE/history $QD_OFFLINE_EGRESS_STATE/dump-failed" >/dev/null
}

# qd_offline_egress_expected_hosts — the host names in QCI_OFFLINE_EGRESS_EXPECTED.
qd_offline_egress_expected_hosts() {
    local t
    for t in $QCI_OFFLINE_EGRESS_EXPECTED; do
        t="${t%@*}"
        case "$t" in */*) ;; *) printf '%s\n' "$t" ;; esac
    done
}

# qd_offline_egress_scan <vm> [vm-exec] — print the live table as JSON
# ("QCI-EGRESS-LIVE <json>"), each history dump ("QCI-EGRESS-HIST <json>"),
# the reference ruleset loaded into a throwaway
# netns in the guest ("QCI-EGRESS-REF <json>") and one
# "QCI-OFFLINE-EGRESS-RESOLVED <host> <addr>" line per address the guest
# resolves an expected host to. rc 2 when the guest cannot be asked or the
# table is not live: the caller must not read that as clean.
qd_offline_egress_scan() {
    local vm="$1" vmx="${2:-${VM_TOOLS:-$QD_OFFLINE_EGRESS_LIBDIR/..}/vm-exec}" out hosts rules rc t=$QD_OFFLINE_EGRESS_TABLE
    hosts="$(qd_offline_egress_expected_hosts | tr '\n' ' ')"
    rules="$(qd_offline_egress_ruleset | base64 -w0)"
    out="$("$vmx" "$vm" "live=\$(nft -j list table inet $t 2>&1) || { echo \"QCI_EGRESS_UNARMED nft: \$live; tables: \$(nft list tables 2>&1 | tr '\n' ' '); unit: \$(systemctl is-active $QD_OFFLINE_EGRESS_UNIT 2>&1); context: \$(id -Z 2>&1); netns self/pid1: \$(readlink /proc/self/ns/net /proc/1/ns/net | tr '\n' ' '); file: \$(ls -Z $QD_OFFLINE_EGRESS_NFT 2>&1); unit log: \$(journalctl -b -u $QD_OFFLINE_EGRESS_UNIT -o cat --no-pager 2>&1 | tail -n 5 | tr '\n' ' ')\"; exit 0; }; echo QCI_EGRESS_ARMED; echo \"QCI-EGRESS-LIVE \$live\"; [ -e $QD_OFFLINE_EGRESS_STATE/dump-failed ] && echo QCI-EGRESS-DUMP-FAILED; for f in $QD_OFFLINE_EGRESS_STATE/history/*.json; do [ -s \"\$f\" ] && echo \"QCI-EGRESS-HIST \$(tr -d '\n' < \"\$f\")\"; done; echo \"QCI-EGRESS-REF \$(printf '%s' '$rules' | base64 -d | unshare -n sh -c 'nft -f - && nft -j list table inet $t' 2>&1 | tr '\n' ' ')\"; for h in $hosts; do getent ahosts \$h 2>/dev/null | awk -v h=\$h '{print \"QCI-OFFLINE-EGRESS-RESOLVED \" h \" \" \$1}' | sort -u; done")" || {
        rc=$?
        printf 'offline-egress: asking %s failed (rc %s): %s\n' "$vm" "$rc" "$(printf '%s' "$out" | head -c 2000)" >&2
        return 2
    }
    case "$out" in
        QCI_EGRESS_ARMED*) printf '%s\n' "$out" | sed '1d' | grep -E '^(QCI-EGRESS-LIVE|QCI-EGRESS-HIST|QCI-EGRESS-REF|QCI-OFFLINE-EGRESS-RESOLVED) |^QCI-EGRESS-DUMP-FAILED$' || true ;;
        *) printf 'offline-egress: %s is not armed: %s\n' "$vm" "$(printf '%s' "$out" | head -c 2000)" >&2
           return 2 ;;
    esac
}

# qd_offline_egress_classify — stdin: scan output. Prints "broken <why>" when
# the live table is missing, unparsable or differs from the reference, else
# one "expected <dst> <attempts>" / "unexpected <dst> <attempts>" line per
# recorded destination (offline-egress-classify.py has the rules).
qd_offline_egress_classify() {
    QCI_OFFLINE_EGRESS_EXPECTED="$QCI_OFFLINE_EGRESS_EXPECTED" \
    QD_OFFLINE_EGRESS_SET_SIZE="$QD_OFFLINE_EGRESS_SET_SIZE" \
        python3 "$QD_OFFLINE_EGRESS_LIBDIR/offline-egress-classify.py"
}
