#!/usr/bin/env bats
# qci:host-only
#
# Host-only contract tests for scripts/vm/lib/offline-egress.sh — the guest
# egress block that makes QCI_OFFLINE enforced rather than routing-only —
# and its gate side in ci/lib/vm.sh / ci/lib/run.sh:
#   - the ruleset rejects public destinations at once, passes private ones,
#     and re-applies idempotently (checked in an unprivileged user+net ns)
#   - the guest installer fails closed without nft
#   - the scan reports only post-cursor block lines; unarmed is not clean
#   - classification, the failed row, and the run-rc escalation

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    LIB="$REPO_ROOT/scripts/vm/lib/offline-egress.sh"
    [ -f "$LIB" ]
    unset QCI_OFFLINE QDISTRO_DEPS_OFFLINE QCI_OFFLINE_EGRESS_EXPECTED
    # shellcheck source=/dev/null
    . "$LIB"
    FAKE="$BATS_TEST_TMPDIR/vm-exec"
}

userns_net_ok() {
    unshare -rn true 2>/dev/null && [ -x /usr/sbin/nft ]
}

@test "ruleset: public TCP is reset at once and counted; private and loopback pass" {
    userns_net_ok || skip "needs unprivileged user+net namespaces and /usr/sbin/nft"
    qd_offline_egress_ruleset > "$BATS_TEST_TMPDIR/r.nft"
    run unshare -rn bash -c "
        ip link set lo up; ip link add d0 type dummy; ip link set d0 up
        ip addr add 10.9.9.2/24 dev d0; ip route add default via 10.9.9.1
        /usr/sbin/nft -f '$BATS_TEST_TMPDIR/r.nft' || exit 9
        /usr/sbin/nft -f '$BATS_TEST_TMPDIR/r.nft' || exit 8   # idempotent re-apply
        timeout 3 bash -c 'exec 3<>/dev/tcp/192.0.2.1/443' 2>/dev/null; echo pub=\$?
        timeout 1 bash -c 'exec 3<>/dev/tcp/10.9.9.7/80' 2>/dev/null; echo priv=\$?
        /usr/sbin/nft list counter inet qci_offline_egress blocked | sed -n 's/.*packets \([0-9]*\).*/blocked=\1/p'
        /usr/sbin/nft list tables | grep -c qci_offline_egress"
    [ "$status" -eq 0 ]
    # Refused (rc 1) — not a timeout (124): the reject is immediate.
    grep -qx 'pub=1' <<<"$output"
    # The private address has no host behind it: it times out (124), i.e. the
    # table let it through to the (absent) peer instead of rejecting it.
    grep -qx 'priv=124' <<<"$output"
    grep -qx 'blocked=1' <<<"$output"
    [ "$(tail -1 <<<"$output")" = 1 ]
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

@test "scan: unarmed guest and unreachable guest are errors, not clean" {
    printf '#!/bin/bash\necho QCI_EGRESS_UNARMED\n' > "$FAKE"; chmod +x "$FAKE"
    run qd_offline_egress_scan vm "$FAKE"
    [ "$status" -eq 2 ]
    printf '#!/bin/bash\nexit 1\n' > "$FAKE"
    run qd_offline_egress_scan vm "$FAKE"
    [ "$status" -eq 2 ]
}

@test "scan: armed guest prints only the block lines it was asked for" {
    cat > "$FAKE" <<'EOF'
#!/bin/bash
grep -q -- "--after-cursor=" <<<"$2" || { echo "scan did not start at the cursor" >&2; exit 7; }
echo QCI_EGRESS_ARMED
echo '2026-10-12T01:00:00+0000 vm kernel: QCI-OFFLINE-EGRESS: IN= OUT=ens2 SRC=10.0.2.15 DST=195.135.223.226 LEN=60 PROTO=TCP DPT=443'
echo '-- No entries --'
EOF
    chmod +x "$FAKE"
    run qd_offline_egress_scan vm "$FAKE"
    [ "$status" -eq 0 ]
    [ "$(grep -c . <<<"$output")" -eq 1 ]
    grep -q 'DST=195.135.223.226 ' <<<"$output"
    printf '#!/bin/bash\necho QCI_EGRESS_ARMED\n' > "$FAKE"
    run qd_offline_egress_scan vm "$FAKE"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "classify: declared chatter hosts are expected, every other destination is not" {
    QCI_OFFLINE_EGRESS_EXPECTED=localhost
    run qd_offline_egress_classify <<'EOF'
k: QCI-OFFLINE-EGRESS: OUT=ens2 SRC=10.0.2.15 DST=127.0.0.1 LEN=60
k: QCI-OFFLINE-EGRESS: OUT=ens2 SRC=10.0.2.15 DST=198.51.100.7 LEN=60
k: QCI-OFFLINE-EGRESS: OUT=ens2 SRC=10.0.2.15 DST=198.51.100.7 LEN=52
EOF
    [ "$status" -eq 0 ]
    grep -qx 'expected 127.0.0.1' <<<"$output"
    grep -qx 'unexpected 198.51.100.7' <<<"$output"
    [ "$(grep -c . <<<"$output")" -eq 2 ]
}

@test "classify: uid scope, proto/port, IPv6 canonical form and guest-resolved addresses" {
    QCI_OFFLINE_EGRESS_EXPECTED="weather.invalid@1000 udp/123"
    run qd_offline_egress_classify <<'EOF'
QCI-OFFLINE-EGRESS-RESOLVED weather.invalid 2001:db8::7
QCI-OFFLINE-EGRESS-RESOLVED weather.invalid 198.51.100.9
k: QCI-OFFLINE-EGRESS: OUT=ens2 SRC=fe80::1 DST=2001:0db8:0000:0000:0000:0000:0000:0007 LEN=80 PROTO=TCP SPT=1 DPT=443 UID=1000 GID=1000
k: QCI-OFFLINE-EGRESS: OUT=ens2 SRC=10.0.2.15 DST=198.51.100.9 LEN=60 PROTO=TCP SPT=1 DPT=443 UID=0 GID=0
k: QCI-OFFLINE-EGRESS: OUT=ens2 SRC=10.0.2.15 DST=203.0.113.5 LEN=76 PROTO=UDP SPT=1 DPT=123
k: QCI-OFFLINE-EGRESS: OUT=ens2 SRC=10.0.2.15 DST=203.0.113.6 LEN=60 PROTO=TCP SPT=1 DPT=123 UID=0 GID=0
EOF
    [ "$status" -eq 0 ]
    grep -qx 'expected 2001:db8::7' <<<"$output"         # expanded kernel form matched, uid 1000
    grep -qx 'unexpected 198.51.100.9' <<<"$output"      # same host, but sent by root
    grep -qx 'expected 203.0.113.5' <<<"$output"         # NTP
    grep -qx 'unexpected 203.0.113.6' <<<"$output"       # tcp/123 is not udp/123
    [ "$(grep -c . <<<"$output")" -eq 4 ]
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
    RDIR="$BATS_TEST_TMPDIR/run"; mkdir -p "$RDIR" "$BATS_TEST_TMPDIR/tools/lib"
    printf 'gate\tsubject\tstatus\texit_code\texit_class\tkind\tlog\tnotes\tcategory\n' > "$RDIR/results.tsv"
    VM_TOOLS="$BATS_TEST_TMPDIR/tools"
    cp "$LIB" "$VM_TOOLS/lib/"
    EXIT_BUILD=20
    record_result() { printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t\n' "$@" >> "$RDIR/results.tsv"; }
    log() { echo "$*" >&2; }
    # shellcheck source=/dev/null
    eval "$(sed -n '/^qci_offline_egress_check() {/,/^}/p' "$REPO_ROOT/ci/lib/vm.sh")"
    # shellcheck source=/dev/null
    eval "$(sed -n '/^offline_egress_rc() {/,/^}/p' "$REPO_ROOT/ci/lib/run.sh")"
    declare -F qci_offline_egress_check offline_egress_rc >/dev/null
}

@test "gate: an unexpected blocked destination is a failed row and fails a passing run" {
    gate_env
    cat > "$VM_TOOLS/vm-exec" <<'EOF'
#!/bin/bash
echo QCI_EGRESS_ARMED
echo 'k: QCI-OFFLINE-EGRESS: OUT=ens2 SRC=10.0.2.15 DST=198.51.100.7 LEN=60'
EOF
    chmod +x "$VM_TOOLS/vm-exec"
    QCI_OFFLINE=1 QCI_OFFLINE_EGRESS_EXPECTED=localhost qci_offline_egress_check qci-bats-x
    run awk -F'\t' '$1 == "offline-egress" && $2 == "qci-bats-x" && $3 == "fail"' "$RDIR/results.tsv"
    grep -q '198.51.100.7' <<<"$output"
    [ -s "$RDIR/offline-egress/qci-bats-x.log" ]
    [ "$(offline_egress_rc 0)" = 20 ]
    [ "$(offline_egress_rc 35)" = 35 ]
}

@test "gate: expected chatter only, or a clean guest, records no failure" {
    gate_env
    cat > "$VM_TOOLS/vm-exec" <<'EOF'
#!/bin/bash
echo QCI_EGRESS_ARMED
echo 'k: QCI-OFFLINE-EGRESS: OUT=ens2 SRC=10.0.2.15 DST=127.0.0.1 LEN=60'
EOF
    chmod +x "$VM_TOOLS/vm-exec"
    QCI_OFFLINE=1 QCI_OFFLINE_EGRESS_EXPECTED=localhost qci_offline_egress_check qci-gui-y
    ! grep -q '^offline-egress' "$RDIR/results.tsv"
    [ "$(offline_egress_rc 0)" = 0 ]
}

@test "gate: an unarmed guest is a failed row; online runs are not scanned" {
    gate_env
    printf '#!/bin/bash\necho QCI_EGRESS_UNARMED\n' > "$VM_TOOLS/vm-exec"; chmod +x "$VM_TOOLS/vm-exec"
    qci_offline_egress_check qci-bats-online
    ! grep -q '^offline-egress' "$RDIR/results.tsv"
    QCI_OFFLINE=1 qci_offline_egress_check qci-bats-z
    run awk -F'\t' '$1 == "offline-egress" && $2 == "qci-bats-z" && $3 == "fail"' "$RDIR/results.tsv"
    grep -q 'not armed' <<<"$output"
}

@test "wiring: the spinner arms offline guests and fails closed; release_vm, golden and finish_run check" {
    grep -q 'qd_offline_egress_install "\$VM"' "$REPO_ROOT/scripts/vm/spin-test-vm.sh"
    grep -q 'could not install the guest egress block"; exit 3' "$REPO_ROOT/scripts/vm/spin-test-vm.sh"
    [ "$(grep -c '^ *qci_offline_egress_check "\$g\?vm"$' "$REPO_ROOT/ci/lib/vm.sh")" -eq 2 ]
    grep -q '^ *rc=\$(offline_egress_rc "\$rc")$' "$REPO_ROOT/ci/lib/run.sh"
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
