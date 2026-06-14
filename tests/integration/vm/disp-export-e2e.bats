#!/usr/bin/env bats
# Export-back e2e — the REAL podman + real-broker + real-resolver half of the
# disposable export-back flow (07-disposables-plan P2 / D7 copy-exception). The
# host lanes (tests/unit/test_disposable_export.py + test_tier2_spawn.py +
# test_disposable_classes.py) prove the promoter (all-or-nothing / caps /
# O_NOFOLLOW / receipt), the store import_from_disposable fail-closed paths, and
# the spawn-side plan/RW-bind/labels against fakes; this suite swaps in the
# SHIPPED /usr/bin/qdistro-tier2-spawn on real rootless podman, the real admin
# broker qdistro.dispose.export:<class> gate (at BOTH spawn and import), and the
# real qdistro-resolve-binding — the half the headless dev host cannot run.
#
# The heavy lifting lives in tests/integration/vm/probes/disp-export-probe.sh
# (staged to /root by fresh-vm-bootstrap.sh). It drives the SHIPPED binary + the
# daemon's own modules so a packaging gap surfaces.
#
# Order is load-bearing: setup_file builds the image + authors the spawn/open/
# export rules + checks the compositor; each test is self-contained (spawn ->
# assert -> teardown); teardown_file removes the rules + staging.

load helpers

PROBE="/root/disp-export-probe.sh"

setup_file() {
    vm_run "[ -f $PROBE ]"
    assert_success || fail_loud "probe not staged at $PROBE (fresh-vm-bootstrap.sh probes step)"
    vm_run "command -v podman >/dev/null"
    assert_success || fail_loud "podman not available — export-back cannot spawn"
    vm_run "[ -x /usr/bin/qdistro-tier2-spawn ]"
    assert_success || fail_loud "/usr/bin/qdistro-tier2-spawn not installed (PACKAGING GAP)"
    vm_run "[ -f /usr/libexec/qdistro/qdistro_disposable_export.py ]"
    assert_success || fail_loud "export promoter not installed (PACKAGING GAP)"
    vm_run "[ -d /var/lib/qdistro/disposable-export ]"
    assert_success || fail_loud "export staging base not created by install (PACKAGING GAP)"
    vm_run "bash $PROBE setup"
    assert_success
    assert_output_contains "PASS: setup"
}

teardown_file() {
    vm_run "bash $PROBE teardown"
    assert_success || fail_loud "disp-export-probe teardown failed (test-authored rules/staging may persist)"
    assert_output_contains "PASS: teardown"
}

@test "export-back: export-enabled spawn binds /mnt/output RW, writes through, meta outside bind, labels stamped" {
    vm_run "bash $PROBE export-rw-mount"
    assert_success
    assert_output_contains "PASS: export-enabled disposable spawned"
    assert_output_contains "PASS: /mnt/output bound READ-WRITE"
    assert_output_contains "PASS: export labels stamped"
    assert_output_contains "PASS: staging tree created; meta.json outside the payload bind"
    assert_output_contains "PASS: in-container write to /mnt/output reached the host staging payload"
    assert_output_contains "PASS: export-rw-mount"
}

@test "export-back: export-capable open with no export rule is refused at the export gate, no container, no staging" {
    vm_run "bash $PROBE export-gate-fail-closed"
    assert_success
    assert_output_contains "PASS: broker returns 'unknown' for the now-unruled export class"
    assert_output_contains "PASS: export-capable open refused at the export gate (decision=unknown)"
    assert_output_contains "PASS: no container minted on the export-gate deny path (fail-closed)"
    assert_output_contains "PASS: no staging tree created on the export-gate deny path"
    assert_output_contains "PASS: export-gate-fail-closed"
}

@test "export-back: import fail-closed via the real broker+resolver (malformed/absent/untemplated/gate-deny)" {
    vm_run "bash $PROBE import-flow"
    assert_success
    assert_output_contains "PASS: import: malformed/absent/untemplated handled via the REAL broker+resolver"
    assert_output_contains "PASS: import: export-gate DENY refused at the REAL broker, staging kept (fail-closed)"
    assert_output_contains "PASS: import-flow"
}

@test "edit-round-trip: edit launch binds /mnt/input RO + /mnt/output RW, stamps qdistro_edit + edit_mode/input_realpath meta" {
    vm_run "bash $PROBE edit-rw-mount"
    assert_success
    assert_output_contains "PASS: edit disposable spawned"
    assert_output_contains "PASS: edit disposable: /mnt/input RO + /mnt/output RW"
    assert_output_contains "PASS: qdistro_edit label stamped"
    assert_output_contains "PASS: meta.json: edit_mode=true + input_realpath stamped outside the bind"
    assert_output_contains "PASS: edit-rw-mount"
}

@test "edit-round-trip: import lands <name>.disp-edited beside source (real-fs, root, silo-owned) + untemplated fail-closed" {
    vm_run "bash $PROBE edit-import"
    assert_success
    assert_output_contains "PASS: edit import: untemplated target refused via the REAL broker+resolver, staging kept"
    assert_output_contains "PASS: edit landing on real fs as root: <name>.disp-edited beside source, silo-owned, source intact, no litter"
    assert_output_contains "PASS: edit-import"
}

# The positive resolve->land glue: a REAL provisioned templated silo whose
# state_path the REAL qdistro-resolve-binding resolves, then a REAL
# import_from_disposable LANDS into it. The fail-closed half (untemplated/gate-
# deny) is proven above; this is the one un-glued backend edge — every other
# real-podman/broker/resolver edge was already VM-proven.
@test "templated-silo import: lands into <state>/Incoming via the REAL resolver + chain-anchored lineage receipts that verify (provisioned binding+generation, silo-owned)" {
    vm_run "bash $PROBE import-land-templated"
    assert_success
    assert_output_contains "PASS: REAL qdistro-resolve-binding resolves the provisioned silo to its state_path"
    assert_output_contains "PASS: import: landed silo-owned + _receipt.json + broker-sealed lineage receipt surfaces (sidecar+manifest+xattr), structural upstream/downstream edges, unresolved snapshot assertions, forged copy refused, staging one-shot removed"
    assert_output_contains "PASS: import-land-templated"
}

@test "templated-silo edit: lands <name>.disp-edited beside a source IN A REAL SILO STATE via the REAL resolver + a chain-anchored sidecar that verifies" {
    vm_run "bash $PROBE edit-land-templated"
    assert_success
    assert_output_contains "PASS: edit import: landed <name>.disp-edited beside source IN A REAL SILO STATE via the REAL resolver, silo-owned, source intact, one broker-sealed sidecar (no manifest), structural upstream/downstream edges, unresolved snapshot assertions, xattr, staging one-shot removed"
    assert_output_contains "PASS: edit-land-templated"
}
