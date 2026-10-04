#!/usr/bin/env bats
#
# Host-only test for the bats gate's `# qci:enforcing` lane
# (ci/lib/gates/bats.sh, ci/lib/vm.sh): a marked tests/integration/vm/*.bats
# file runs on a clone of the per-run golden booted SELinux=enforcing and
# reached over SSH, never on a permissive VM. No VM, no libvirt.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    TMP="$(mktemp -d)"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/core.sh"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/bootstrap.sh"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/vm.sh"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/bats.sh"
    RDIR="$TMP/run"; mkdir -p "$RDIR/vm"
    EXIT_BATS=35; EXIT_VM_PROVISION=${EXIT_VM_PROVISION:-30}
    record_result() { printf '%s\t' "$@" >> "$TMP/rows"; echo >> "$TMP/rows"; }
    record_timing() { :; }
}

teardown() { [ -n "${TMP:-}" ] && rm -rf "$TMP"; }

@test "enforcing marker detected (bare and with a trailing comment)" {
    printf '#!/usr/bin/env bats\n# qci:enforcing\n' > "$TMP/a.bats"
    printf '#!/usr/bin/env bats\n# qci:enforcing — SSH\n' > "$TMP/b.bats"
    run bats_is_enforcing "$TMP/a.bats"; [ "$status" -eq 0 ]
    run bats_is_enforcing "$TMP/b.bats"; [ "$status" -eq 0 ]
}

@test "no marker, a look-alike, or a marker past line 40 is not enforcing" {
    printf '#!/usr/bin/env bats\nload helpers\n' > "$TMP/a.bats"
    printf '#!/usr/bin/env bats\n# qci:enforcingish\n#  qci:enforcing\n' > "$TMP/b.bats"
    { printf '#!/usr/bin/env bats\n'; for _ in $(seq 1 45); do echo '#'; done; echo '# qci:enforcing'; } > "$TMP/c.bats"
    for f in a b c; do
        run bats_is_enforcing "$TMP/$f.bats"
        [ "$status" -ne 0 ]
    done
}

@test "an enforcing file without an SSH port is refused, not run permissive" {
    qci_assert_vm_exists() { return 0; }
    printf '#!/usr/bin/env bats\n# qci:enforcing\n@test x { touch "%s/ran"; }\n' "$TMP" > "$TMP/e.bats"
    run bats_run_one qci-fake "$TMP/e.bats" ""
    [ "$status" -eq 35 ]
    [ ! -e "$TMP/ran" ]
    grep -q "marked # qci:enforcing but no SSH port" "$TMP/rows"
}

@test "disposable runner routes enforcing files to the enforcing clone with its port" {
    acquire_vm() { echo "permissive-called" >> "$TMP/calls"; echo qci-permissive; }
    acquire_enforcing_vm() { echo "enforcing-called $1" >> "$TMP/calls"; echo "qci-enf-vm 34567"; }
    bats_run_one() { echo "run_one $1 $(basename "$2") port=${3:-}" >> "$TMP/calls"; return 0; }
    release_vm() { :; }
    bats_skip_if_sibling_app_missing() { return 1; }
    printf '#!/usr/bin/env bats\n# qci:enforcing\n' > "$TMP/enf.bats"
    printf '#!/usr/bin/env bats\nload helpers\n' > "$TMP/perm.bats"
    run bats_run_disposable "$TMP/enf.bats"; [ "$status" -eq 0 ]
    run bats_run_disposable "$TMP/perm.bats"; [ "$status" -eq 0 ]
    grep -qx "enforcing-called bats-enf" "$TMP/calls"
    grep -qx "run_one qci-enf-vm enf.bats port=34567" "$TMP/calls"
    grep -qx "run_one qci-permissive perm.bats port=" "$TMP/calls"
    [ "$(grep -c permissive-called "$TMP/calls")" -eq 1 ]
}

@test "enforcing acquire without the per-run golden fails provisioning" {
    RUN_GOLDEN_BATS=""
    run acquire_enforcing_vm bats-x
    [ "$status" -eq "$EXIT_VM_PROVISION" ]
    grep -q "qci:enforcing needs the per-run bats golden" "$TMP/rows"
}

@test "presentation-enforcing.bats is marked enforcing and not host-only" {
    f="$REPO_ROOT/tests/integration/vm/presentation-enforcing.bats"
    bats_is_enforcing "$f"
    ! bats_is_host_only "$f"
}

@test "clone-baseweed --enforcing requires a run golden" {
    run bash "$REPO_ROOT/scripts/vm/clone-baseweed.sh" qci-x --enforcing
    [ "$status" -eq 2 ]
    [[ "$output" == *"--enforcing requires --from-run-golden"* ]]
}
