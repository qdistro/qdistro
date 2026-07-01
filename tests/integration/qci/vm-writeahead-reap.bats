#!/usr/bin/env bats
#
# Host-only test for the write-ahead orphan reaper (ci/lib/vm.sh, H2/H3): a
# domain the spinner created but that never reached created-vms.txt — because the
# name could not be parsed (H2) or a worker was killed mid-provision — must still
# be reaped at end of run, WITHOUT ever touching a pre-existing / concurrent-run
# VM (VM names are only gate-prefixed, not run-unique, so the baseline diff is the
# shared-host safety guard).
#
# No libvirt is touched: VIRSH is stubbed by a fake `virsh` on PATH backed by a
# tiny domain-registry file, so destroy/undefine are observable and list --all
# --name reflects a synthetic host.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    TMP="$(mktemp -d)"
    RDIR="$TMP/run"; mkdir -p "$RDIR/vm/provisioning.d"
    : > "$RDIR/vm/created-vms.txt"
    DOMREG="$TMP/domains.txt"          # one domain name per line = "defined" hosts
    REAPED="$TMP/reaped.txt"; : > "$REAPED"

    # Fake virsh: supports `list --all --name`, `destroy <d>`, `undefine <d> ...`,
    # `domblklist <d> --details`. Backed by $DOMREG / logs to $REAPED.
    BIN="$TMP/bin"; mkdir -p "$BIN"
    cat > "$BIN/virsh" <<EOF
#!/usr/bin/env bash
DOMREG="$DOMREG"; REAPED="$REAPED"
sub=""
# skip a leading -c qemu:///session
args=("\$@")
i=0; while [ \$i -lt \${#args[@]} ]; do
    case "\${args[\$i]}" in -c) i=\$((i+2)); continue ;; esac
    break
done
sub="\${args[\$i]}"; i=\$((i+1))
case "\$sub" in
    list) cat "\$DOMREG" 2>/dev/null ;;
    destroy) echo "destroy \${args[\$i]}" >> "\$REAPED" ;;
    undefine) echo "undefine \${args[\$i]}" >> "\$REAPED"
              grep -vFx "\${args[\$i]}" "\$DOMREG" > "\$DOMREG.tmp" 2>/dev/null || true
              mv "\$DOMREG.tmp" "\$DOMREG" 2>/dev/null || true ;;
    domblklist) echo "" ;;
    *) : ;;
esac
exit 0
EOF
    chmod +x "$BIN/virsh"
    VIRSH=("$BIN/virsh")

    # Stubs / minimal env for the sourced module.
    EXIT_VM_PROVISION=40; EXIT_OK=0
    QDWIN_IMG_DIR="$TMP/images"; mkdir -p "$QDWIN_IMG_DIR"
    log() { :; }
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/vm.sh"
}

teardown() { [ -n "${TMP:-}" ] && rm -rf "$TMP"; }

@test "reap_new_orphans: reaps a NEW untracked domain matching the prefix" {
    printf '%s\n' "qci-bats-x-OLD" "qci-bats-x-NEW" > "$DOMREG"
    # OLD existed at baseline; NEW appeared after -> only NEW is an orphan.
    reap_new_orphans "qci-bats-x-" "qci-bats-x-OLD,"
    grep -q "undefine qci-bats-x-NEW" "$REAPED"
    ! grep -q "qci-bats-x-OLD" "$REAPED"
}

@test "reap_new_orphans: never touches a pre-existing / concurrent-run VM (baseline)" {
    printf '%s\n' "qci-bats-x-CONCURRENT" > "$DOMREG"
    # The domain was already present at baseline -> excluded, even though it
    # matches the prefix (a concurrent run's VM on a shared host).
    reap_new_orphans "qci-bats-x-" "qci-bats-x-CONCURRENT,"
    [ ! -s "$REAPED" ]
}

@test "reap_new_orphans: never reaps a TRACKED (recorded) domain" {
    printf '%s\n' "qci-bats-x-NEW" > "$DOMREG"
    echo "qci-bats-x-NEW" >> "$RDIR/vm/created-vms.txt"
    reap_new_orphans "qci-bats-x-" ""
    [ ! -s "$REAPED" ]
}

@test "reap_writeahead_orphans: reaps orphan from a LEFTOVER marker (kill mid-provision)" {
    # Simulate a worker killed mid-provision: a .wa marker survives, the domain it
    # created is running, but it never reached created-vms.txt.
    printf '%s\n' "qci-gui-s1-OLD" "qci-gui-s1-ORPHAN" > "$DOMREG"
    printf 'prefix\tqci-gui-s1-\nbaseline\tqci-gui-s1-OLD,\n' > "$RDIR/vm/provisioning.d/worker1-123.wa"
    reap_writeahead_orphans
    grep -q "undefine qci-gui-s1-ORPHAN" "$REAPED"
    ! grep -q "qci-gui-s1-OLD" "$REAPED"
    # Marker is consumed (idempotent).
    [ ! -e "$RDIR/vm/provisioning.d/worker1-123.wa" ]
}

@test "reap_writeahead_orphans: no markers -> no-op" {
    printf '%s\n' "qci-gui-s1-SOMETHING" > "$DOMREG"
    reap_writeahead_orphans
    [ ! -s "$REAPED" ]
}
