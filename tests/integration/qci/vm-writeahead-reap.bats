#!/usr/bin/env bats
#
# Host-only test for the write-ahead orphan reaper (ci/lib/vm.sh, H2/H3 +
# FINDING 2): a domain the spinner created but that never reached created-vms.txt
# — because the name could not be parsed (H2) or a worker was killed
# mid-provision — must still be reaped at end of run, WITHOUT ever touching a
# pre-existing / concurrent-run VM. VM names are only gate-prefixed, not
# run-unique, so two guards protect a shared host:
#   (a) baseline diff — a domain present before this run's spinner is excluded;
#   (b) provisioning-window guard (FINDING 2) — only a domain whose embedded
#       YYMMDD-HHMMSS timestamp (minted by clone-baseweed.sh) falls inside THIS
#       acquire's [win_start-margin, win_end+margin] is reaped, so a concurrent
#       same-prefix run that created its domain AFTER our baseline snapshot is
#       still rejected. An unparseable name is NEVER reaped (fail safe).
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

    NOW=$(date +%s)
    WIN_START=$((NOW - 300)); WIN_END=$((NOW + 300))
}

teardown() { [ -n "${TMP:-}" ] && rm -rf "$TMP"; }

# Build a spinner-style domain name: <prefix>YYMMDD-HHMMSS-<pid>-<rand> at $epoch.
ts_name() {
    local prefix=$1 epoch=$2
    printf '%s%s-%d-%d' "$prefix" "$(date -d @"$epoch" +%y%m%d-%H%M%S)" 12345 6789
}

@test "reap_new_orphans: reaps a NEW in-window untracked domain matching the prefix" {
    local old new
    old=$(ts_name "qci-bats-x-" "$WIN_START")
    new=$(ts_name "qci-bats-x-" "$NOW")
    printf '%s\n' "$old" "$new" > "$DOMREG"
    # OLD existed at baseline; NEW appeared after -> only NEW is an orphan.
    reap_new_orphans "qci-bats-x-" "$old," "$WIN_START" "$WIN_END"
    grep -q "undefine $new" "$REAPED"
    ! grep -q "$old" "$REAPED"
}

@test "reap_new_orphans: never touches a pre-existing / concurrent-run VM (baseline)" {
    local dom
    dom=$(ts_name "qci-bats-x-" "$NOW")
    printf '%s\n' "$dom" > "$DOMREG"
    # The domain was already present at baseline -> excluded, even though it
    # matches the prefix AND is in-window (a concurrent run's VM on a shared host).
    reap_new_orphans "qci-bats-x-" "$dom," "$WIN_START" "$WIN_END"
    [ ! -s "$REAPED" ]
}

@test "reap_new_orphans: never reaps a TRACKED (recorded) domain" {
    local dom
    dom=$(ts_name "qci-bats-x-" "$NOW")
    printf '%s\n' "$dom" > "$DOMREG"
    echo "$dom" >> "$RDIR/vm/created-vms.txt"
    reap_new_orphans "qci-bats-x-" "" "$WIN_START" "$WIN_END"
    [ ! -s "$REAPED" ]
}

@test "reap_new_orphans: OUT-OF-WINDOW domain (concurrent run) is NOT reaped" {
    local past
    # A same-prefix domain created long before this acquire's window: a concurrent
    # run that started earlier. Not in baseline (appeared after our snapshot is not
    # the case here, but its timestamp proves it is not ours).
    past=$(ts_name "qci-bats-x-" "$((NOW - 100000))")
    printf '%s\n' "$past" > "$DOMREG"
    reap_new_orphans "qci-bats-x-" "" "$WIN_START" "$WIN_END"
    [ ! -s "$REAPED" ]
}

@test "reap_new_orphans: UNPARSEABLE name is NOT reaped (fail safe)" {
    # A domain whose remainder after the prefix is not YYMMDD-HHMMSS-... : never
    # touched when a window is enforced, even though baseline + tracked would pass.
    printf '%s\n' "qci-bats-x-WEIRD-NAME" > "$DOMREG"
    reap_new_orphans "qci-bats-x-" "" "$WIN_START" "$WIN_END"
    [ ! -s "$REAPED" ]
}

@test "reap_new_orphans: no window supplied falls back to baseline-only (unparseable reaped)" {
    # Backward-compat: with an empty window the parse/window gate is skipped.
    printf '%s\n' "qci-bats-x-NEW" > "$DOMREG"
    reap_new_orphans "qci-bats-x-" "" "" ""
    grep -q "undefine qci-bats-x-NEW" "$REAPED"
}

@test "reap_writeahead_orphans: reaps in-window orphan from a LEFTOVER marker (kill mid-provision)" {
    local old orphan
    old=$(ts_name "qci-gui-s1-" "$WIN_START")
    orphan=$(ts_name "qci-gui-s1-" "$NOW")
    printf '%s\n' "$old" "$orphan" > "$DOMREG"
    printf 'prefix\tqci-gui-s1-\nbaseline\t%s,\nwin_start\t%s\n' "$old" "$WIN_START" \
        > "$RDIR/vm/provisioning.d/worker1-123.wa"
    reap_writeahead_orphans
    grep -q "undefine $orphan" "$REAPED"
    ! grep -q "$old" "$REAPED"
    # Marker is consumed (idempotent).
    [ ! -e "$RDIR/vm/provisioning.d/worker1-123.wa" ]
}

@test "reap_writeahead_orphans: marker window rejects a concurrent run's out-of-window domain" {
    # A same-prefix domain minted BEFORE this dead worker's win_start belongs to a
    # concurrent run and must survive the leftover-marker sweep.
    local concurrent
    concurrent=$(ts_name "qci-gui-s1-" "$((WIN_START - 100000))")
    printf '%s\n' "$concurrent" > "$DOMREG"
    printf 'prefix\tqci-gui-s1-\nbaseline\t\nwin_start\t%s\n' "$WIN_START" \
        > "$RDIR/vm/provisioning.d/worker1-123.wa"
    reap_writeahead_orphans
    [ ! -s "$REAPED" ]
    [ ! -e "$RDIR/vm/provisioning.d/worker1-123.wa" ]
}

@test "reap_writeahead_orphans: no markers -> no-op" {
    printf '%s\n' "$(ts_name "qci-gui-s1-" "$NOW")" > "$DOMREG"
    reap_writeahead_orphans
    [ ! -s "$REAPED" ]
}

@test "vm_name_epoch: parses embedded timestamp, rejects unparseable" {
    local e
    e=$(vm_name_epoch "$(ts_name "qci-bats-x-" "$NOW")" "qci-bats-x-")
    [ "$e" = "$NOW" ]
    run vm_name_epoch "qci-bats-x-WEIRD" "qci-bats-x-"
    [ "$status" -ne 0 ]
    [ -z "$output" ]
}
