#!/usr/bin/env bats
# qci:host-only
#
# Host-only contract tests for scripts/vm/lib/offline-egress.sh — the guest
# egress block that makes QCI_OFFLINE enforced rather than routing-only —
# and its gate side in ci/lib/vm.sh / ci/lib/run.sh / ci/lib/gates/mmnet.sh:
#   - the ruleset rejects public destinations at once, records every attempt
#     (destination, proto, port, uid) in its sets, passes private ones, and
#     re-applies idempotently (checked in an unprivileged user+net ns)
#   - a live table that differs from the reference ruleset is "broken"
#   - the guest installer fails closed without nft; the boot guard starts
#     guests with their links down and fails closed when that does not take
#   - the scan returns rc 2 for an unarmed/unreachable guest
#   - classification (uid scope, proto/port, IPv6 forms, guest resolution,
#     forward-confirmed reverse names, set overflow), the failed rows, and
#     the run-rc escalation

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    LIB="$REPO_ROOT/scripts/vm/lib/offline-egress.sh"
    [ -f "$LIB" ]
    unset QCI_OFFLINE QDISTRO_DEPS_OFFLINE QCI_OFFLINE_EGRESS_EXPECTED QD_OFFLINE_EGRESS_ARM
    # shellcheck source=/dev/null
    . "$LIB"
    FAKE="$BATS_TEST_TMPDIR/vm-exec"
}

userns_net_ok() {
    unshare -rn true 2>/dev/null && [ -x /usr/sbin/nft ]
}

# A scan transcript from a real table: load the ruleset in a user+net ns,
# make the attempts given as bash /dev/tcp|udp targets, dump it (LIVE), and a
# fresh copy in another ns (REF). $1 = extra nft commands run after the load
# (tampering), rest = "tcp|udp host port" triples.
real_scan() {
    local tamper="$1"; shift
    qd_offline_egress_ruleset > "$BATS_TEST_TMPDIR/r.nft"
    local tries="$BATS_TEST_TMPDIR/tries.sh" a
    : > "$tries"
    for a in "$@"; do
        set -- $a
        case "$1" in
            # An IP packet with no transport header (protocol 253).
            raw) printf 'import socket, sys\nsocket.socket(socket.AF_INET, socket.SOCK_RAW, 253).sendto(b"", (sys.argv[1], 0))\n' > "$BATS_TEST_TMPDIR/raw.py"
                 printf 'python3 %q %q 2>/dev/null\n' "$BATS_TEST_TMPDIR/raw.py" "$2" >> "$tries" ;;
            *) printf "timeout 2 bash -c 'echo x > /dev/%s/%s/%s' 2>/dev/null\n" "$1" "$2" "$3" >> "$tries" ;;
        esac
    done
    unshare -rn bash -c "
        PATH=/usr/sbin:\$PATH
        ip link set lo up; ip link add d0 type dummy; ip link set d0 up
        ip addr add 10.9.9.2/24 dev d0; ip route add default via 10.9.9.1
        nft -f '$BATS_TEST_TMPDIR/r.nft' || exit 9
        nft -f '$BATS_TEST_TMPDIR/r.nft' || exit 8
        bash '$BATS_TEST_TMPDIR/tries.sh'
        $tamper
        echo \"QCI-EGRESS-LIVE \$(nft -j list table inet qci_offline_egress)\""
    unshare -rn bash -c "PATH=/usr/sbin:\$PATH; nft -f '$BATS_TEST_TMPDIR/r.nft' && echo \"QCI-EGRESS-REF \$(nft -j list table inet qci_offline_egress)\""
}

# A synthetic transcript: a table whose recording sets hold the given
# elements (JSON arrays), with the matching reference (same table, empty sets).
synth_scan() {
    python3 - "$@" <<'PYX'
import json, sys
sets = {"any4": [], "any6": [], "out4": [], "out6": [], "fwd4": [], "fwd6": []}
for arg in sys.argv[1:]:
    name, elems = arg.split("=", 1)
    sets[name] = json.loads(elems)
def table(with_elems):
    objs = [{"metainfo": {"version": "x"}}, {"table": {"family": "inet", "name": "qci_offline_egress"}}]
    for n, e in sets.items():
        s = {"family": "inet", "name": n, "table": "qci_offline_egress", "flags": ["dynamic"], "size": 65535}
        if with_elems and e:
            s["elem"] = e
            s["count"] = len(e)   # newer nft reports it on a non-empty set
        objs.append({"set": s})
    return json.dumps({"nftables": objs})
print("QCI-EGRESS-LIVE " + table(True))
print("QCI-EGRESS-REF " + table(False))
PYX
}

@test "ruleset: public traffic is refused at once and recorded with proto, port and uid; private passes" {
    userns_net_ok || skip "needs unprivileged user+net namespaces and /usr/sbin/nft"
    qd_offline_egress_ruleset > "$BATS_TEST_TMPDIR/r.nft"
    run unshare -rn bash -c "
        PATH=/usr/sbin:\$PATH
        ip link set lo up; ip link add d0 type dummy; ip link set d0 up
        ip addr add 10.9.9.2/24 dev d0; ip route add default via 10.9.9.1
        nft -f '$BATS_TEST_TMPDIR/r.nft' || exit 9
        nft -f '$BATS_TEST_TMPDIR/r.nft' || exit 8   # idempotent re-apply
        timeout 3 bash -c 'exec 3<>/dev/tcp/192.0.2.1/443' 2>/dev/null; echo pub=\$?
        timeout 1 bash -c 'exec 3<>/dev/tcp/10.9.9.7/80' 2>/dev/null; echo priv=\$?
        timeout 1 bash -c 'exec 3<>/dev/tcp/100.100.100.100/53' 2>/dev/null; echo cgnat=\$?
        nft list counter inet qci_offline_egress blocked | sed -n 's/.*packets \([0-9]*\).*/blocked=\1/p'
        nft -j list set inet qci_offline_egress out4
        nft -j list set inet qci_offline_egress any4
        nft list tables | grep -c qci_offline_egress"
    [ "$status" -eq 0 ]
    # Refused (rc 1) — not a timeout (124): the reject is immediate.
    grep -qx 'pub=1' <<<"$output"
    # The private address has no host behind it: it times out (124), i.e. the
    # table let it through to the (absent) peer instead of rejecting it.
    grep -qx 'priv=124' <<<"$output"
    grep -qx 'cgnat=124' <<<"$output"    # 100.64/10: passt's host resolver
    grep -qx 'blocked=1' <<<"$output"
    grep -q '"val": {"concat": \["192.0.2.1", "tcp", 443, 0\]}, "counter": {"packets": 1' <<<"$output"
    grep -q '"val": "192.0.2.1", "counter": {"packets": 1' <<<"$output"
    ! grep -q '10.9.9.7' <<<"$output"
    [ "$(tail -1 <<<"$output")" = 1 ]
}

@test "classify on a real table: recorded attempts classified; NTP expected (IPv6 forms: synthetic test below)" {
    userns_net_ok || skip "needs unprivileged user+net namespaces and /usr/sbin/nft"
    real_scan "" "tcp 192.0.2.1 443" "udp 198.51.100.4 123" > "$BATS_TEST_TMPDIR/scan"
    QCI_OFFLINE_EGRESS_EXPECTED="udp/123" run qd_offline_egress_classify < "$BATS_TEST_TMPDIR/scan"
    [ "$status" -eq 0 ]
    grep -qx 'unexpected 192.0.2.1 \[tcp/443@0\]' <<<"$output"
    grep -qx 'expected 198.51.100.4 \[udp/123@0\]' <<<"$output"
    [ "$(grep -c . <<<"$output")" -eq 2 ]
}

@test "classify on a real table: a packet with no port is counted, so its destination is never expected" {
    userns_net_ok || skip "needs unprivileged user+net namespaces and /usr/sbin/nft"
    real_scan "" "udp 198.51.100.4 123" "raw 198.51.100.4" > "$BATS_TEST_TMPDIR/scan"
    QCI_OFFLINE_EGRESS_EXPECTED="udp/123" run qd_offline_egress_classify < "$BATS_TEST_TMPDIR/scan"
    [ "$status" -eq 0 ]
    grep -qx 'unexpected 198.51.100.4 \[udp/123@0,no-detail\]' <<<"$output"
}

@test "classify on a real table: a tampered live table is broken, never clean" {
    userns_net_ok || skip "needs unprivileged user+net namespaces and /usr/sbin/nft"
    for tamper in \
        "nft insert rule inet qci_offline_egress output accept" \
        "nft add element inet qci_offline_egress allow4 '{ 192.0.2.0/24 }'" \
        "nft flush chain inet qci_offline_egress block" \
        "nft delete chain inet qci_offline_egress forward"; do
        real_scan "$tamper" > "$BATS_TEST_TMPDIR/scan"
        run qd_offline_egress_classify < "$BATS_TEST_TMPDIR/scan"
        [ "$status" -eq 0 ]
        grep -q '^broken live table differs' <<<"$output" || { echo "not caught: $tamper: $output" >&2; false; }
    done
    real_scan "" > "$BATS_TEST_TMPDIR/scan"
    run qd_offline_egress_classify < "$BATS_TEST_TMPDIR/scan"
    [ -z "$output" ]
}

@test "guest installer fails closed when the guest has no nft" {
    mkdir -p "$BATS_TEST_TMPDIR/bin"
    for t in bash base64 mkdir; do ln -s "$(command -v "$t")" "$BATS_TEST_TMPDIR/bin/$t"; done
    cat > "$FAKE" <<EOF
#!/bin/bash
PATH="$BATS_TEST_TMPDIR/bin" exec bash -c "\$2"
EOF
    chmod +x "$FAKE"
    run qd_offline_egress_install vm "$FAKE"
    [ "$status" -eq 3 ]
    grep -q 'nft not installed' <<<"$output"
}

@test "boot guard: start paused, links down and verified, then resume; refuses when a link stays up or NICs cannot be listed" {
    mkdir -p "$BATS_TEST_TMPDIR/bin"
    cat > "$BATS_TEST_TMPDIR/bin/virsh" <<'EOF'
#!/bin/bash
shift 2   # -c qemu:///session
echo "$*" >> "$VIRSH_LOG"
case "$1" in
    domiflist) [ -n "${LIST_FAILS:-}" ] && exit 1
               [ -n "${NO_NICS:-}" ] && { printf ' Interface   Type   Source   Model    MAC\n------\n'; exit 0; }
               printf ' Interface   Type   Source   Model    MAC\n------\n -   user   -   virtio   52:54:00:aa:bb:01\n -   udp   -   virtio   52:54:00:aa:bb:02\n' ;;
    domif-getlink) [ -n "${STUCK_UP:-}" ] && echo "$3 up" || echo "$3 ${LINK:-down}" ;;
esac
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/virsh"
    export VIRSH_LOG="$BATS_TEST_TMPDIR/virsh.log" PATH="$BATS_TEST_TMPDIR/bin:$PATH"
    run qd_offline_egress_start_dark vm1
    [ "$status" -eq 0 ]
    [ "$(cat "$VIRSH_LOG")" = "start --paused vm1
domiflist vm1
domif-setlink vm1 52:54:00:aa:bb:01 down
domif-getlink vm1 52:54:00:aa:bb:01
domif-setlink vm1 52:54:00:aa:bb:02 down
domif-getlink vm1 52:54:00:aa:bb:02
resume vm1" ]
    for fault in STUCK_UP NO_NICS LIST_FAILS; do
        : > "$VIRSH_LOG"
        env "$fault=1" bash -c '. "$1"; qd_offline_egress_start_dark vm1' _ "$LIB" && { echo "$fault: started" >&2; false; }
        grep -qx 'destroy vm1' "$VIRSH_LOG" || { echo "$fault: not destroyed" >&2; false; }
        ! grep -q '^resume' "$VIRSH_LOG"
    done
}

@test "scan: unarmed guest and unreachable guest are errors, not clean" {
    printf '#!/bin/bash\necho QCI_EGRESS_UNARMED\n' > "$FAKE"; chmod +x "$FAKE"
    run qd_offline_egress_scan vm "$FAKE"
    [ "$status" -eq 2 ]
    printf '#!/bin/bash\nexit 1\n' > "$FAKE"
    run qd_offline_egress_scan vm "$FAKE"
    [ "$status" -eq 2 ]
}

@test "scan: armed guest passes on only the table, reference and resolution lines" {
    cat > "$FAKE" <<'EOF'
#!/bin/bash
grep -q -- "unshare -n" <<<"$2" || { echo "scan did not build a reference" >&2; exit 7; }
echo QCI_EGRESS_ARMED
echo 'QCI-EGRESS-LIVE {"nftables": []}'
echo 'QCI-EGRESS-REF {"nftables": []}'
echo 'QCI-OFFLINE-EGRESS-RESOLVED github.com 140.82.121.3'
echo 'some noise'
EOF
    chmod +x "$FAKE"
    run qd_offline_egress_scan vm "$FAKE"
    [ "$status" -eq 0 ]
    [ "$(grep -c . <<<"$output")" -eq 3 ]
    ! grep -q noise <<<"$output"
}

@test "classify: uid scope, proto/port, IPv6 forms, guest-resolved addresses, no-detail and forwarded" {
    synth_scan \
        'any4=["198.51.100.9", "203.0.113.5", "203.0.113.6", "203.0.113.8", "203.0.113.9"]' \
        'any6=["2001:db8::7"]' \
        'out4=[{"concat": ["198.51.100.9", "tcp", 443, 0]}, {"concat": ["203.0.113.5", "udp", 123, 479]}, {"concat": ["203.0.113.6", "tcp", 123, 0]}]' \
        'out6=[{"concat": ["2001:0db8:0000:0000:0000:0000:0000:0007", "tcp", 443, 1000]}]' \
        'fwd4=[{"concat": ["203.0.113.9", "tcp", 443]}]' > "$BATS_TEST_TMPDIR/scan"
    printf 'QCI-OFFLINE-EGRESS-RESOLVED weather.invalid 2001:db8::7\nQCI-OFFLINE-EGRESS-RESOLVED weather.invalid 198.51.100.9\nQCI-OFFLINE-EGRESS-RESOLVED weather.invalid 203.0.113.9\n' >> "$BATS_TEST_TMPDIR/scan"
    QCI_OFFLINE_EGRESS_EXPECTED="weather.invalid@1000 udp/123" run qd_offline_egress_classify < "$BATS_TEST_TMPDIR/scan"
    [ "$status" -eq 0 ]
    grep -qx 'expected 2001:db8::7 \[tcp/443@1000\]' <<<"$output"      # expanded form, uid 1000
    grep -qx 'unexpected 198.51.100.9 \[tcp/443@0\]' <<<"$output"      # same host, sent by root
    grep -qx 'expected 203.0.113.5 \[udp/123@479\]' <<<"$output"       # NTP
    grep -qx 'unexpected 203.0.113.6 \[tcp/123@0\]' <<<"$output"       # tcp/123 is not udp/123
    grep -qx 'unexpected 203.0.113.8 \[no-detail\]' <<<"$output"       # blocked, no detail recorded
    grep -qx 'unexpected 203.0.113.9 \[tcp/443@fwd\]' <<<"$output"     # forwarded: no uid to scope
    [ "$(grep -c . <<<"$output")" -eq 6 ]
}

@test "classify: a reverse name counts only when it resolves back to the destination" {
    synth_scan 'any4=["127.0.0.1"]' 'out4=[{"concat": ["127.0.0.1", "tcp", 443, 1000]}]' > "$BATS_TEST_TMPDIR/scan"
    getent hosts 127.0.0.1 | grep -qw localhost || skip "127.0.0.1 has no localhost reverse name here"
    QCI_OFFLINE_EGRESS_EXPECTED="localhost@1000" run qd_offline_egress_classify < "$BATS_TEST_TMPDIR/scan"
    grep -qx 'expected 127.0.0.1 \[tcp/443@1000\]' <<<"$output"
    # A PTR name that does not resolve back must not count.
    synth_scan 'any4=["192.0.2.55"]' 'out4=[{"concat": ["192.0.2.55", "tcp", 443, 1000]}]' > "$BATS_TEST_TMPDIR/scan"
    printf '{"ptr": {"192.0.2.55": "lb-1.github.com"}, "fwd": {"lb-1.github.com": ["140.82.121.3"]}}' > "$BATS_TEST_TMPDIR/dns.json"
    QD_OFFLINE_EGRESS_FAKE_DNS="$BATS_TEST_TMPDIR/dns.json" QCI_OFFLINE_EGRESS_EXPECTED="github.com@1000" run qd_offline_egress_classify < "$BATS_TEST_TMPDIR/scan"
    grep -qx 'unexpected 192.0.2.55 \[tcp/443@1000\]' <<<"$output"
    # ... and one that does, does.
    printf '{"ptr": {"192.0.2.55": "lb-1.github.com"}, "fwd": {"lb-1.github.com": ["192.0.2.55"]}}' > "$BATS_TEST_TMPDIR/dns.json"
    QD_OFFLINE_EGRESS_FAKE_DNS="$BATS_TEST_TMPDIR/dns.json" QCI_OFFLINE_EGRESS_EXPECTED="github.com@1000" run qd_offline_egress_classify < "$BATS_TEST_TMPDIR/scan"
    grep -qx 'expected 192.0.2.55 \[tcp/443@1000\]' <<<"$output"
    # Host-side forward resolution includes IPv6 (no AI_ADDRCONFIG), in the
    # canonical form, so a dual-stack guest's v6 attempt matches the host.
    synth_scan 'any6=["2001:db8::9"]' 'out6=[{"concat": ["2001:0db8::0009", "tcp", 443, 1000]}]' > "$BATS_TEST_TMPDIR/scan"
    printf '{"fwd": {"weather.invalid": ["198.51.100.1", "2001:db8:0:0::9"]}}' > "$BATS_TEST_TMPDIR/dns.json"
    QD_OFFLINE_EGRESS_FAKE_DNS="$BATS_TEST_TMPDIR/dns.json" QCI_OFFLINE_EGRESS_EXPECTED="weather.invalid@1000" run qd_offline_egress_classify < "$BATS_TEST_TMPDIR/scan"
    grep -qx 'expected 2001:db8::9 \[tcp/443@1000\]' <<<"$output"
}

@test "classify: attempts saved in the history before a reload or reboot still count" {
    synth_scan > "$BATS_TEST_TMPDIR/scan"
    synth_scan 'any4=["192.0.2.77"]' 'out4=[{"concat": ["192.0.2.77", "tcp", 443, 0]}]' \
        | sed -n 's/^QCI-EGRESS-LIVE /QCI-EGRESS-HIST /p' >> "$BATS_TEST_TMPDIR/scan"
    run qd_offline_egress_classify < "$BATS_TEST_TMPDIR/scan"
    [ "$status" -eq 0 ]
    [ "$output" = 'unexpected 192.0.2.77 [tcp/443@0]' ]
    echo 'QCI-EGRESS-HIST {torn' >> "$BATS_TEST_TMPDIR/scan"
    run qd_offline_egress_classify < "$BATS_TEST_TMPDIR/scan"
    grep -q '^broken a saved history dump is not JSON' <<<"$output"
}

@test "every reload path saves the live table first; a golden is sealed after its scan" {
    unit="$(qd_offline_egress_unit)"
    grep -qx 'Conflicts=shutdown.target' <<<"$unit"
    # openSUSE flushes the whole ruleset at boot (soft-reboot-cleanup), so the
    # table must load after it, and still before the network.
    grep -qx 'After=local-fs.target soft-reboot-cleanup.service' <<<"$unit"
    grep -q '^Before=network-pre.target' <<<"$unit"
    grep -q '^Before=.*shutdown.target' <<<"$unit"
    grep -q '^ExecStop=.*sealed ] || {.*nft -j list table inet qci_offline_egress > ' <<<"$unit"
    grep -q 'date +%%s%%N' <<<"$unit"                     # % escaped for systemd
    grep -qx 'ExecStartPre=-/bin/rm -f /var/lib/qci-offline-egress/sealed' <<<"$unit"
    script="$(qd_offline_egress_guest_script)"
    # The installer dumps, then always loads (whatever the live table was).
    [ "$(grep -n 'nft -j list table' <<<"$script" | cut -d: -f1)" -lt "$(grep -n '^nft -f' <<<"$script" | cut -d: -f1)" ]
    ! grep -q 'cmp -s' <<<"$script"
    n=$(grep -n 'qci_offline_egress_check "\$gvm"' "$REPO_ROOT/ci/lib/vm.sh" | cut -d: -f1)
    sed -n "$n,$((n + 4))p" "$REPO_ROOT/ci/lib/vm.sh" | grep -q 'qd_offline_egress_seal "\$gvm"'
    # ... and its links go down before that final scan.
    sed -n "$((n - 6)),$((n - 1))p" "$REPO_ROOT/ci/lib/vm.sh" | grep -q 'qd_offline_egress_set_links "\$gvm" down'
}

@test "a table that cannot be saved before a reload is sticky-broken, and the installer refuses to reload" {
    synth_scan > "$BATS_TEST_TMPDIR/scan"
    echo QCI-EGRESS-DUMP-FAILED >> "$BATS_TEST_TMPDIR/scan"
    run qd_offline_egress_classify < "$BATS_TEST_TMPDIR/scan"
    grep -q '^broken the guest could not save its table' <<<"$output"
    # The dump: table present but unlistable as JSON -> marker + failure;
    # no table -> nothing to save, success.
    mkdir -p "$BATS_TEST_TMPDIR/bin" "$BATS_TEST_TMPDIR/state"
    printf '#!/bin/bash\ncase "$*" in "list table inet qci_offline_egress") exit ${NO_TABLE:-0} ;; *-j*) exit 1 ;; esac\n' > "$BATS_TEST_TMPDIR/bin/nft"
    chmod +x "$BATS_TEST_TMPDIR/bin/nft"
    cmd="$(QD_OFFLINE_EGRESS_STATE="$BATS_TEST_TMPDIR/state" qd_offline_egress_dump_cmd)"
    run env PATH="$BATS_TEST_TMPDIR/bin:$PATH" sh -c "$cmd"
    [ "$status" -ne 0 ]
    [ -e "$BATS_TEST_TMPDIR/state/dump-failed" ]
    rm -f "$BATS_TEST_TMPDIR/state/dump-failed"
    run env NO_TABLE=1 PATH="$BATS_TEST_TMPDIR/bin:$PATH" sh -c "$cmd"
    [ "$status" -eq 0 ]
    [ ! -e "$BATS_TEST_TMPDIR/state/dump-failed" ]
    # In the installer the failed dump stops the script before nft -f.
    script="$(qd_offline_egress_guest_script)"
    grep -q '^set -eu$' <<<"$script"
    grep -q 'touch /var/lib/qci-offline-egress/dump-failed; false; }; fi$' <<<"$script"
}

@test "classify: missing, unparsable or overflowing tables are broken" {
    run qd_offline_egress_classify <<<"QCI-OFFLINE-EGRESS-RESOLVED x 1.2.3.4"
    grep -q '^broken no live table' <<<"$output"
    run qd_offline_egress_classify <<<"QCI-EGRESS-LIVE {nope"
    grep -q '^broken live table is not JSON' <<<"$output"
    synth_scan 'any4=["192.0.2.1"]' > "$BATS_TEST_TMPDIR/scan"
    sed -i 's/^QCI-EGRESS-REF .*/QCI-EGRESS-REF Error: unshare failed/' "$BATS_TEST_TMPDIR/scan"
    run qd_offline_egress_classify < "$BATS_TEST_TMPDIR/scan"
    grep -q '^broken the guest could not load the reference' <<<"$output"
    synth_scan 'any4=["192.0.2.1", "192.0.2.2"]' > "$BATS_TEST_TMPDIR/scan"
    QD_OFFLINE_EGRESS_SET_SIZE=2 run qd_offline_egress_classify < "$BATS_TEST_TMPDIR/scan"
    grep -q '^broken set any4 is full' <<<"$output"
}

@test "ruleset logs the sending uid; the default expected list scopes web hosts to the session user" {
    grep -q 'log prefix "QCI-OFFLINE-EGRESS: " level warn flags skuid' <<<"$(qd_offline_egress_ruleset)"
    for t in $QCI_OFFLINE_EGRESS_EXPECTED; do
        case "$t" in */*|*@1000) ;; *) echo "unscoped expected host: $t" >&2; false ;; esac
    done
}

# The gate side: source the real vm.sh / run.sh functions with the runner's
# primitives stubbed, against a fake guest.
gate_env() {
    RDIR="$BATS_TEST_TMPDIR/run"; mkdir -p "$RDIR/vm" "$BATS_TEST_TMPDIR/tools/lib"
    printf 'gate\tsubject\tstatus\texit_code\texit_class\tkind\tlog\tnotes\tcategory\n' > "$RDIR/results.tsv"
    VM_TOOLS="$BATS_TEST_TMPDIR/tools"
    cp "$LIB" "$REPO_ROOT/scripts/vm/lib/offline-egress-classify.py" "$VM_TOOLS/lib/"
    EXIT_BUILD=20
    record_result() { printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t\n' "$@" >> "$RDIR/results.tsv"; }
    log() { echo "$*" >&2; }
    # shellcheck source=/dev/null
    eval "$(sed -n '/^qci_offline_egress_check() {/,/^}/p' "$REPO_ROOT/ci/lib/vm.sh")"
    # shellcheck source=/dev/null
    eval "$(sed -n '/^offline_egress_rc() {/,/^}/p' "$REPO_ROOT/ci/lib/run.sh")"
    declare -F qci_offline_egress_check offline_egress_rc >/dev/null
}

fake_guest() {   # $1 = file holding the transcript the fake guest prints
    printf '#!/bin/bash\necho QCI_EGRESS_ARMED\ncat %q\n' "$1" > "$VM_TOOLS/vm-exec"
    chmod +x "$VM_TOOLS/vm-exec"
}

@test "gate: an unexpected blocked destination is a failed row and fails a passing run" {
    gate_env
    synth_scan 'any4=["198.51.100.7"]' 'out4=[{"concat": ["198.51.100.7", "tcp", 443, 0]}]' > "$BATS_TEST_TMPDIR/t"
    fake_guest "$BATS_TEST_TMPDIR/t"
    QCI_OFFLINE=1 QCI_OFFLINE_EGRESS_EXPECTED=udp/123 qci_offline_egress_check qci-bats-x
    run awk -F'\t' '$1 == "offline-egress" && $2 == "qci-bats-x" && $3 == "fail"' "$RDIR/results.tsv"
    grep -q '198.51.100.7 \[tcp/443@0\] past the host cache' <<<"$output"
    [ -s "$RDIR/offline-egress/qci-bats-x.log" ]
    [ "$(offline_egress_rc 0)" = 20 ]
    [ "$(offline_egress_rc 35)" = 35 ]
}

@test "gate: expected chatter only, or a clean guest, records no failure" {
    gate_env
    synth_scan 'any4=["203.0.113.5"]' 'out4=[{"concat": ["203.0.113.5", "udp", 123, 479]}]' > "$BATS_TEST_TMPDIR/t"
    fake_guest "$BATS_TEST_TMPDIR/t"
    QCI_OFFLINE=1 QCI_OFFLINE_EGRESS_EXPECTED=udp/123 qci_offline_egress_check qci-gui-y
    synth_scan > "$BATS_TEST_TMPDIR/t"
    QCI_OFFLINE=1 qci_offline_egress_check qci-gui-z
    ! grep -q '^offline-egress' "$RDIR/results.tsv"
    [ "$(offline_egress_rc 0)" = 0 ]
}

@test "gate: unarmed, broken and online" {
    gate_env
    printf '#!/bin/bash\necho QCI_EGRESS_UNARMED\n' > "$VM_TOOLS/vm-exec"; chmod +x "$VM_TOOLS/vm-exec"
    qci_offline_egress_check qci-bats-online
    ! grep -q '^offline-egress' "$RDIR/results.tsv"
    QCI_OFFLINE=1 qci_offline_egress_check qci-bats-z
    run awk -F'\t' '$1 == "offline-egress" && $2 == "qci-bats-z" && $3 == "fail"' "$RDIR/results.tsv"
    grep -q 'not armed' <<<"$output"
    echo 'QCI-EGRESS-LIVE {nope' > "$BATS_TEST_TMPDIR/t"; fake_guest "$BATS_TEST_TMPDIR/t"
    QCI_OFFLINE=1 qci_offline_egress_check qci-bats-w
    run awk -F'\t' '$1 == "offline-egress" && $2 == "qci-bats-w" && $3 == "fail"' "$RDIR/results.tsv"
    grep -q 'guest egress block not intact: live table is not JSON' <<<"$output"
}

@test "gate: an enforcing clone is scanned over SSH on its recorded port" {
    gate_env
    printf 'qci-bats-enf-1\t40022\n' > "$RDIR/vm/ssh-ports.tsv"
    printf '#!/bin/bash\nexit 99\n' > "$VM_TOOLS/vm-exec"; chmod +x "$VM_TOOLS/vm-exec"
    mkdir -p "$BATS_TEST_TMPDIR/bin"
    printf '#!/bin/bash\necho "$*" > %q\necho QCI_EGRESS_UNARMED\n' "$BATS_TEST_TMPDIR/ssh.args" > "$BATS_TEST_TMPDIR/bin/ssh"
    chmod +x "$BATS_TEST_TMPDIR/bin/ssh"
    PATH="$BATS_TEST_TMPDIR/bin:$PATH" QCI_OFFLINE=1 qci_offline_egress_check qci-bats-enf-1
    grep -q -- '-p 40022 .*root@127.0.0.1 live=' "$BATS_TEST_TMPDIR/ssh.args"
    grep -q $'^offline-egress\tqci-bats-enf-1\tfail' "$RDIR/results.tsv"
}

@test "wiring: clones boot dark offline; spinner opts in and verifies; release_vm, golden, mmnet and finish_run check" {
    grep -q '^qd_offline_egress_enabled && export QD_OFFLINE_EGRESS_ARM=1$' "$REPO_ROOT/scripts/vm/spin-test-vm.sh"
    grep -q 'could not install the guest egress block"; exit 3' "$REPO_ROOT/scripts/vm/spin-test-vm.sh"
    grep -q 'qd_offline_egress_start_dark "\$VM"' "$REPO_ROOT/scripts/vm/clone-baseweed.sh"
    grep -q 'qd_offline_egress_links_up "\$VM"' "$REPO_ROOT/scripts/vm/clone-baseweed.sh"
    [ "$(grep -c '^ *qci_offline_egress_check "\$g\?vm"$' "$REPO_ROOT/ci/lib/vm.sh")" -eq 2 ]
    grep -q 'qci_offline_egress_check "\$v"' "$REPO_ROOT/ci/lib/gates/mmnet.sh"
    [ "$(grep -c 'QD_OFFLINE_EGRESS_ARM=1 "\$clone"' "$REPO_ROOT/ci/lib/gates/mmnet.sh")" -eq 2 ]
    grep -q 'ssh-ports.tsv' "$REPO_ROOT/ci/lib/vm.sh"
    grep -q '^ *rc=\$(offline_egress_rc "\$rc")$' "$REPO_ROOT/ci/lib/run.sh"
    # Image builders never arm (the table must not be sealed into an image).
    ! grep -q 'QD_OFFLINE_EGRESS_ARM' "$REPO_ROOT/scripts/vm/build-baked-baseweed.sh" "$REPO_ROOT/scripts/vm/build-enforcing-baseweed.sh"
}

@test "registry: suites that fetch past the host cache are skipped offline, others are not" {
    # templates-browser/-promotion build silo images in-guest with zypper
    # against registry.opensuse.org (no cache proxy in the container build):
    # under the egress block they can only fail, so offline runs skip them.
    # shellcheck source=/dev/null
    . "$REPO_ROOT/ci/lib/affected.sh"
    REGISTRY_TSV="$REPO_ROOT/tests/registry.tsv"
    for t in templates-browser templates-promotion; do
        QCI_OFFLINE=1 offline_should_skip_external "tests/integration/vm/$t.bats"
        ! QCI_OFFLINE=0 offline_should_skip_external "tests/integration/vm/$t.bats"
    done
    ! QCI_OFFLINE=1 offline_should_skip_external tests/integration/vm/templates-state-snapshot.bats
}
