#!/usr/bin/env bats
#
# Host-only test for the bats gate's `# qci:host-only` lane
# (ci/lib/gates/bats.sh): a marked tests/integration/vm/*.bats file runs on the
# host instead of taking a disposable VM. The marker is checked, not trusted:
# a marked file that loads the VM helpers, calls vm_run or expands $VM_NAME is
# refused. No VM, no libvirt.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    TMP="$(mktemp -d)"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/core.sh"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/bootstrap.sh"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/bats.sh"
}

teardown() { [ -n "${TMP:-}" ] && rm -rf "$TMP"; }

@test "marker detected (bare and with a trailing comment)" {
    printf '#!/usr/bin/env bats\n# qci:host-only\n@test "x" { true; }\n' > "$TMP/a.bats"
    printf '#!/usr/bin/env bats\n# qci:host-only — no VM\n' > "$TMP/b.bats"
    run bats_is_host_only "$TMP/a.bats"; [ "$status" -eq 0 ]
    run bats_is_host_only "$TMP/b.bats"; [ "$status" -eq 0 ]
}

@test "no marker, a look-alike, or a marker past line 40 is not host-only" {
    printf '#!/usr/bin/env bats\nload helpers\n' > "$TMP/a.bats"
    printf '#!/usr/bin/env bats\n# qci:host-onlyish\n#  qci:host-only\n' > "$TMP/b.bats"
    { printf '#!/usr/bin/env bats\n'; for _ in $(seq 1 45); do echo '#'; done; echo '# qci:host-only'; } > "$TMP/c.bats"
    for f in a b c; do
        run bats_is_host_only "$TMP/$f.bats"
        [ "$status" -ne 0 ]
    done
}

@test "violation: a marked file that needs a VM is refused" {
    printf '# qci:host-only\nload helpers\n' > "$TMP/a.bats"
    printf '# qci:host-only\n@test x { vm_run "true"; }\n' > "$TMP/b.bats"
    printf '# qci:host-only\n@test x { run vm-exec "$VM_NAME" true; }\n' > "$TMP/c.bats"
    printf '# qci:host-only\n@test x { echo "${VM_NAME:-}"; }\n' > "$TMP/d.bats"
    for f in a b c d; do
        run bats_host_only_violation "$TMP/$f.bats"
        [ "$status" -eq 0 ]
        [[ "$output" == *"needs a VM"* ]]
    done
}

@test "no violation for comments or VM_NAME used as plain data" {
    printf '# qci:host-only\n# load helpers is not used here; vm_run neither\n@test x { env VM_NAME=vm-42 true; }\n' > "$TMP/a.bats"
    run bats_host_only_violation "$TMP/a.bats"
    [ "$status" -ne 0 ]
    [ -z "$output" ]
}

@test "every marked file in the repo is VM-free, and the audited set is marked" {
    local f marked=0
    for f in "$REPO_ROOT"/tests/integration/vm/*.bats; do
        bats_is_host_only "$f" || continue
        marked=$((marked + 1))
        run bats_host_only_violation "$f"
        [ "$status" -ne 0 ] || { echo "$f: $output"; return 1; }
    done
    # todo/test-audit-261002/audit-vm-bats.md: the 18 files that spent a VM
    # they never used. A file dropping its marker silently goes back to a VM.
    for f in admin-app-install bootstrap-hardening bootstrap-idempotency \
             bootstrap-installer-resume build-guard codex-packaging-fixes \
             edit-guard gen-source-manifest guest-image-perms image-release \
             kiwi-ci-base offline-install presentation-delivery \
             presentation-docs presentation-four-apps qsu-binary \
             source-manifest-signature \
             spin-test-vm-gui-bootstrap tier5b-ops-hardening; do
        [ -f "$REPO_ROOT/tests/integration/vm/$f.bats" ] || continue
        bats_is_host_only "$REPO_ROOT/tests/integration/vm/$f.bats" \
            || { echo "$f.bats lost its # qci:host-only marker"; return 1; }
    done
    [ "$marked" -ge 18 ]
}

@test "bats_run_host refuses a violating file without running it" {
    RDIR="$TMP/run"; mkdir -p "$RDIR"; EXIT_BATS=35
    record_result() { printf '%s\t' "$@" >> "$TMP/rows"; echo >> "$TMP/rows"; }
    record_timing() { :; }
    printf '#!/usr/bin/env bats\n# qci:host-only\nload helpers\n@test x { touch "%s/ran"; }\n' "$TMP" > "$TMP/v.bats"
    run bats_run_host "$TMP/v.bats"
    [ "$status" -eq 35 ]
    [ ! -e "$TMP/ran" ]
    grep -q $'^bats\tv.bats\tfail\t35' "$TMP/rows"
}

@test "bats_run_host runs a clean file on the host without VM_NAME" {
    RDIR="$TMP/run"; mkdir -p "$RDIR"; EXIT_BATS=35; QDISTRO_REPO="$REPO_ROOT"; QCI_OFFLINE=0
    record_result() { printf '%s\t' "$@" >> "$TMP/rows"; echo >> "$TMP/rows"; }
    record_timing() { :; }
    log() { :; }
    cat > "$TMP/ok.bats" <<EOF
#!/usr/bin/env bats
# qci:host-only
@test "no VM name leaks in" { [ -z "\$(printenv VM_NAME)" ]; }
@test "skipped one" { skip "because"; }
EOF
    VM_NAME=leak run bats_run_host "$TMP/ok.bats"
    [ "$status" -eq 0 ]
    grep -q $'^bats\tok.bats\tpass\t0\tpass\tbats\t[^\t]*\tHOST skipped_cases=1' "$TMP/rows"
    grep -q $'^bats\tok.bats (skipped cases)\tskip' "$TMP/rows"
}
