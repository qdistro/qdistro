#!/usr/bin/env bats
#
# Host-only unit tests for the qci CI-runner capabilities added in
# feat/qci-capabilities:
#   - changed-path -> gate selection (`qci affected`)
#   - `qci replay <scenario> <vm>`
#   - QCI_OFFLINE=1 host-only / no-egress plumbing
#
# These DO NOT boot a VM. They drive the real qci runner with QCI_RUNS_DIR
# pointed at a temp dir and (for replay) stub `virsh` + `bats` on PATH so the
# dispatch path can be asserted without touching libvirt. Selection-only
# `qci affected` (no --run) and offline manifest capture are exercised against
# the real runner with no stubs.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    QCI="$REPO_ROOT/ci/bin/qci"
    RUNS="$(mktemp -d)"
    export QCI_RUNS_DIR="$RUNS"
    STUBDIR="$(mktemp -d)"
}

teardown() {
    rm -rf "$RUNS" "$STUBDIR"
}

# Run `qci affected` in selection-only mode and capture the printed gate list
# (the last non-empty stdout line is the space-separated gate set).
affected_gates() {
    "$QCI" affected "$@" 2>/dev/null | awk 'NF{last=$0} END{print last}'
}

# ---------------------------------------------------------------------------
# Changed-path -> gate selection
# ---------------------------------------------------------------------------

@test "affected: registry-listed host path maps to exactly its gate" {
    run affected_gates tests/unit/test_audit.py
    [ "$status" -eq 0 ]
    [ "$output" = "host" ]
}

@test "affected: registry-listed bats path maps to exactly bats" {
    run affected_gates tests/integration/vm/broker-e2e.bats
    [ "$status" -eq 0 ]
    [ "$output" = "bats" ]
}

@test "affected: registry-listed gui scenario maps to exactly gui" {
    run affected_gates tests/integration/permissions-gui/03-qt-admin-app-visual.md
    [ "$status" -eq 0 ]
    [ "$output" = "gui" ]
}

@test "affected: unknown path fails safe to the FULL gate set" {
    run affected_gates some/unknown/component/file.cpp
    [ "$status" -eq 0 ]
    [ "$output" = "lint host release-manifest bootstrap-release-profile image vm-smoke bats gui" ]
}

@test "affected: ci/ prefix selects selftest+lint+host" {
    run affected_gates ci/bin/qci
    [ "$status" -eq 0 ]
    # A change to the runner itself re-runs the host-only runner self-test.
    [ "$output" = "selftest lint host" ]
}

@test "affected: source-manifest + R1 tooling select the release-manifest gate" {
    run affected_gates scripts/install/source-manifest.txt
    [ "$status" -eq 0 ]
    [ "$output" = "release-manifest" ]   # NOT the generic *.txt 'no gate' rule
    run affected_gates scripts/install/verify-source-manifest.sh
    [ "$status" -eq 0 ]
    [ "$output" = "release-manifest" ]
}

@test "affected: the bootstrap installer selects both release gates" {
    run affected_gates scripts/install/qdistro-bootstrap.sh
    [ "$status" -eq 0 ]
    # GATE_ORDER orders release-manifest before bootstrap-release-profile.
    [ "$output" = "release-manifest bootstrap-release-profile" ]
}

@test "affected: a bootstrap-contract bats selects only the host-only profile gate" {
    run affected_gates tests/integration/vm/bootstrap-hardening.bats
    [ "$status" -eq 0 ]
    [ "$output" = "bootstrap-release-profile" ]   # specific rule beats generic vm/*.bats
    # A non-contract vm bats still maps to the VM bats lane.
    run affected_gates tests/integration/vm/broker-e2e.bats
    [ "$status" -eq 0 ]
    [ "$output" = "bats" ]
}

@test "affected: qci self-test bats selects selftest" {
    run affected_gates tests/integration/qci/qci-runner-contract.bats
    [ "$status" -eq 0 ]
    [ "$output" = "selftest" ]
}

@test "affected: image/ prefix selects image" {
    run affected_gates image/verify.sh
    [ "$status" -eq 0 ]
    [ "$output" = "image" ]
}

@test "affected: scripts/vm prefix selects vm-smoke" {
    run affected_gates scripts/vm/vm-exec
    [ "$status" -eq 0 ]
    [ "$output" = "vm-smoke" ]
}

@test "affected: bats-dir path (not in registry) selects bats" {
    run affected_gates tests/integration/vm/app-launcher.bats
    [ "$status" -eq 0 ]
    [ "$output" = "bats" ]
}

@test "affected: docs-only changes fail safe to FULL set, never empty" {
    run affected_gates README.md todo/notes.md docs/x.md
    [ "$status" -eq 0 ]
    # Must NOT silently skip coverage: widen to full instead of emitting "".
    [ "$output" = "lint host release-manifest bootstrap-release-profile image vm-smoke bats gui" ]
}

@test "affected: mixed paths are de-duplicated and ordered by GATE_ORDER" {
    run affected_gates tests/unit/test_audit.py tests/integration/vm/broker-e2e.bats ci/bin/qci
    [ "$status" -eq 0 ]
    # selftest+lint+host from ci/, host from unit, bats from the bats file:
    # de-duplicated and ordered by GATE_ORDER (selftest first).
    [ "$output" = "selftest lint host bats" ]
}

@test "affected: no paths at all fails safe to FULL set" {
    run affected_gates
    [ "$status" -eq 0 ]
    [ "$output" = "lint host release-manifest bootstrap-release-profile image vm-smoke bats gui" ]
}

@test "affected: mapping log + manifest record the selected gates" {
    run "$QCI" affected tests/unit/test_audit.py
    [ "$status" -eq 0 ]
    local mf log
    mf="$(find "$RUNS" -name manifest.txt | head -1)"
    log="$(find "$RUNS" -name affected.log | head -1)"
    grep -q '^affected_gates=host$' "$mf"
    grep -q 'SELECTED GATES: host' "$log"
}

# ---------------------------------------------------------------------------
# QCI_OFFLINE=1 host-only mode
# ---------------------------------------------------------------------------

@test "offline: records source tarball + matching sha256 in manifest" {
    QCI_OFFLINE=1 run "$QCI" affected tests/unit/test_audit.py
    [ "$status" -eq 0 ]
    local mf dir rec act
    mf="$(find "$RUNS" -name manifest.txt | head -1)"
    dir="$(dirname "$mf")"
    grep -q '^qci_offline=1$' "$mf"
    [ -f "$dir/source.tar.gz" ]
    rec="$(grep '^source_sha256=' "$mf" | cut -d= -f2)"
    act="$(sha256sum "$dir/source.tar.gz" | awk '{print $1}')"
    [ -n "$rec" ]
    [ "$rec" = "$act" ]
}

@test "offline: non-offline run records qci_offline=0 and no tarball" {
    run "$QCI" affected tests/unit/test_audit.py
    [ "$status" -eq 0 ]
    local mf dir
    mf="$(find "$RUNS" -name manifest.txt | head -1)"
    dir="$(dirname "$mf")"
    grep -q '^qci_offline=0$' "$mf"
    [ ! -f "$dir/source.tar.gz" ]
}

# ---------------------------------------------------------------------------
# qci replay <scenario> <vm>  (dispatch only; virsh + bats stubbed)
# ---------------------------------------------------------------------------

# Install stubs so validate_vm (virsh dominfo) succeeds and bats/vm-exec are
# captured instead of actually run against a VM. The stub records its argv.
install_replay_stubs() {
    cat > "$STUBDIR/virsh" <<'SH'
#!/usr/bin/env bash
# Succeed for `dominfo replay-vm` and `dominfo qdistro-daily` (exists, but
# protection must still refuse it); fail dominfo for any other VM name so the
# "nonexistent VM" path is exercised. Succeed for list.
case "$*" in
    *dominfo\ replay-vm) exit 0 ;;
    *dominfo\ qdistro-daily) exit 0 ;;
    *dominfo\ *) exit 1 ;;
    *list*) exit 0 ;;
    *) exit 0 ;;
esac
SH
    cat > "$STUBDIR/bats" <<SH
#!/usr/bin/env bash
echo "STUB-BATS argv: \$*" >> "$STUBDIR/calls.log"
echo "STUB-BATS VM_NAME=\${VM_NAME:-} QCI_OFFLINE=\${QCI_OFFLINE:-}" >> "$STUBDIR/calls.log"
exit 0
SH
    chmod +x "$STUBDIR/virsh" "$STUBDIR/bats"
    : > "$STUBDIR/calls.log"
}

@test "replay: missing args is a usage error" {
    run "$QCI" replay
    [ "$status" -eq 2 ]
}

@test "replay: nonexistent VM is rejected (vm-provision class)" {
    install_replay_stubs
    # virsh stub fails dominfo for an unknown VM name (only replay-vm succeeds).
    PATH="$STUBDIR:$PATH" run "$QCI" replay tests/unit/test_audit.py no-such-vm
    # Selection of a non-bats/gui path also fails; but VM check runs first.
    [ "$status" -ne 0 ]
}

@test "replay: resolves bats basename and dispatches to gate_bats with the VM" {
    install_replay_stubs
    # Stub bats lives first on PATH so gate_bats runs the stub, not real bats.
    PATH="$STUBDIR:$PATH" run "$QCI" replay broker-e2e replay-vm
    [ "$status" -eq 0 ]
    # The stub was invoked with the resolved .bats path and the named VM.
    grep -q 'STUB-BATS argv: .*tests/integration/vm/broker-e2e.bats' "$STUBDIR/calls.log"
    grep -q 'VM_NAME=replay-vm' "$STUBDIR/calls.log"
    # Manifest records the resolved scenario + kind + vm.
    local mf
    mf="$(find "$RUNS" -name manifest.txt | head -1)"
    grep -q '^replay_kind=bats$' "$mf"
    grep -q '^replay_vm=replay-vm$' "$mf"
    grep -q 'tests/integration/vm/broker-e2e.bats$' "$mf"
}

@test "replay: resolves explicit .bats path and dispatches" {
    install_replay_stubs
    PATH="$STUBDIR:$PATH" run "$QCI" replay tests/integration/vm/app-launcher.bats replay-vm
    [ "$status" -eq 0 ]
    grep -q 'STUB-BATS argv: .*tests/integration/vm/app-launcher.bats' "$STUBDIR/calls.log"
}

@test "replay: unresolvable scenario is a usage error" {
    install_replay_stubs
    PATH="$STUBDIR:$PATH" run "$QCI" replay does-not-exist-anywhere replay-vm
    [ "$status" -eq 2 ]
}

@test "replay: protected qdistro-daily VM is refused without override" {
    install_replay_stubs
    PATH="$STUBDIR:$PATH" run "$QCI" replay broker-e2e qdistro-daily
    [ "$status" -ne 0 ]
    # bats stub must never have been called for a protected VM.
    [ ! -s "$STUBDIR/calls.log" ] || ! grep -q STUB-BATS "$STUBDIR/calls.log"
}
