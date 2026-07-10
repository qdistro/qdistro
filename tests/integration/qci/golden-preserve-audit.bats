#!/usr/bin/env bats
#
# Host-only test for the GOLDEN_PRESERVE marker-failure audit (ci/lib/vm.sh,
# H12). release_vm sets GOLDEN_PRESERVE=1 inside a backgrounded worker subshell,
# so the parent (cleanup_run_goldens) learns of the preserve ONLY via a marker
# file. If that marker write fails, the parent must NOT delete a golden that a
# preserved overlay still backs. cleanup_run_goldens' belt-and-suspenders is
# golden_has_backing_referrer: even with no marker, a golden with a surviving
# overlay referrer is preserved.
#
# qemu-img is stubbed on PATH: it prints `backing file: <x>` from a sidecar
# `<overlay>.backing` fixture, so backing chains are synthetic (no real qcow2).

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    TMP="$(mktemp -d)"
    RDIR="$TMP/run"; mkdir -p "$RDIR/vm" "$RDIR/host"
    QDWIN_IMG_DIR="$TMP/images"; mkdir -p "$QDWIN_IMG_DIR"
    BIN="$TMP/bin"; mkdir -p "$BIN"
    cat > "$BIN/qemu-img" <<'EOF'
#!/usr/bin/env bash
# usage: qemu-img info <path>  -> print synthetic backing line from <path>.backing
for a in "$@"; do path="$a"; done
[ -f "$path.backing" ] && echo "backing file: $(cat "$path.backing")"
exit 0
EOF
    chmod +x "$BIN/qemu-img"
    PATH="$BIN:$PATH"
    log() { :; }
    EXIT_OK=0
    EXIT_RUNNER=90
    GOLDEN_PRESERVE=0
    RUN_GOLDEN_DISKS=()
    VIRSH=("$BIN/virsh")
    cat > "$BIN/virsh" <<'EOF'
#!/usr/bin/env bash
case "$*" in
    *"list --all --name"*) printf '%s\n' "${VIRSH_DEFINED_NAMES:-}" ;;
esac
exit 0
EOF
    chmod +x "$BIN/virsh"
    qci_assert_run_dir() { :; }
    record_result() { :; }
    exit_class_name() { [ "$1" -eq 0 ] && printf pass || printf runner; }
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/vm.sh"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/cleanup.sh"
}

teardown() { [ -n "${TMP:-}" ] && rm -rf "$TMP"; }

# Make a golden + optionally a referring overlay.
mk_golden() { : > "$QDWIN_IMG_DIR/$1"; printf '%s\n' "$QDWIN_IMG_DIR/$1"; }
mk_overlay() { : > "$QDWIN_IMG_DIR/$1"; echo "$2" > "$QDWIN_IMG_DIR/$1.backing"; }

@test "golden_has_backing_referrer: overlay backed by the golden => referred (0)" {
    local g; g=$(mk_golden "qci-golden-bats-1.qcow2")
    mk_overlay "qci-bats-worker.qcow2" "$g"
    run golden_has_backing_referrer "$g"
    [ "$status" -eq 0 ]
}

@test "golden_has_backing_referrer: no overlay references it => not referred (1)" {
    local g; g=$(mk_golden "qci-golden-bats-1.qcow2")
    mk_overlay "qci-bats-worker.qcow2" "$QDWIN_IMG_DIR/qci-golden-OTHER.qcow2"
    run golden_has_backing_referrer "$g"
    [ "$status" -ne 0 ]
}

@test "golden_has_backing_referrer: overlay with no backing is ignored" {
    local g; g=$(mk_golden "qci-golden-bats-1.qcow2")
    : > "$QDWIN_IMG_DIR/qci-bats-nobacking.qcow2"   # no .backing sidecar
    run golden_has_backing_referrer "$g"
    [ "$status" -ne 0 ]
}

@test "cleanup_run_goldens: DELETES an unreferenced golden with no marker" {
    local g; g=$(mk_golden "qci-golden-bats-1.qcow2")
    RUN_GOLDEN_DISKS=("$g")
    cleanup_run_goldens
    [ ! -e "$g" ]
}

@test "cleanup_run_goldens: PRESERVES a golden a surviving overlay backs, marker ABSENT" {
    local g; g=$(mk_golden "qci-golden-bats-1.qcow2")
    mk_overlay "qci-bats-worker.qcow2" "$g"
    RUN_GOLDEN_DISKS=("$g")
    # No $RDIR/vm/golden-preserve marker and GOLDEN_PRESERVE=0: only the
    # backing-referrer double-check can save it.
    cleanup_run_goldens
    [ -e "$g" ]
    grep -q "preserved_golden_disk=$g" "$RDIR/manifest.txt"
}

@test "cleanup_run_goldens: PRESERVES via the marker even without a referrer" {
    local g; g=$(mk_golden "qci-golden-bats-1.qcow2")
    RUN_GOLDEN_DISKS=("$g")
    : > "$RDIR/vm/golden-preserve"
    cleanup_run_goldens
    [ -e "$g" ]
}

@test "cleanup gate preserves an old undefined disk while an overlay backs it" {
    local g; g=$(mk_golden "qci-golden-bats-test.qcow2")
    mk_overlay "qci-bats-worker.qcow2" "$g"
    VIRSH_DEFINED_NAMES=qci-bats-worker
    export VIRSH_DEFINED_NAMES
    touch -d '2 hours ago' "$g" "$QDWIN_IMG_DIR/qci-bats-worker.qcow2"

    gate_cleanup --age-hours 1

    [ -e "$g" ]
    grep -q 'keep orphan qci-golden-bats-test.qcow2 (backing-referrer audit: referred)' \
        "$RDIR/host/cleanup.log"
}
