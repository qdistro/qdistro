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

# --- gate-side endpoint check (sol r84 finding 2) ---------------------------

_endpoint_stubs() {
    qci_assert_vm_exists() { return 0; }
    collect_vm_artifacts_ssh() { :; }
    scenario_scratch_dir() { printf '%s/scratch' "$TMP"; }
    VIRSH=(_fake_virsh)
    _fake_virsh() {
        printf "<domain><interface type='user'><portForward proto='tcp' address='127.0.0.1'>\n"
        printf "        <range start='%s' to='22'/>\n" "${FAKE_FWD_PORT:-}"
        printf "</portForward></interface></domain>\n"
    }
    qci_enforcing_ssh() { printf '%s\r\n' "${FAKE_MODE:-}"; }
    printf '#!/usr/bin/env bats\n# qci:enforcing\n@test x { touch "%s/ran"; }\n' "$TMP" > "$TMP/e.bats"
}

@test "explicit VM that answers Permissive is refused before the file runs" {
    _endpoint_stubs
    FAKE_FWD_PORT=40001 FAKE_MODE=Permissive run bats_run_one qci-fake "$TMP/e.bats" 40001
    [ "$status" -eq 35 ]
    [ ! -e "$TMP/ran" ]
    grep -q "getenforce=Permissive, not Enforcing" "$TMP/rows"
}

@test "an SSH port the domain does not forward is refused" {
    _endpoint_stubs
    FAKE_FWD_PORT=40002 FAKE_MODE=Enforcing run bats_run_one qci-fake "$TMP/e.bats" 40001
    [ "$status" -eq 35 ]
    [ ! -e "$TMP/ran" ]
    grep -q "SSH port 40001 is not forwarded to guest :22 by domain qci-fake" "$TMP/rows"
}

@test "a forwarded, Enforcing endpoint runs the marked file with VM_SSH_PORT" {
    _endpoint_stubs
    QDISTRO_REPO="$TMP"; QCI_OFFLINE=0
    printf '#!/usr/bin/env bats\n# qci:enforcing\n@test x { echo "$VM_SSH_PORT" > "%s/ran"; }\n' "$TMP" > "$TMP/e.bats"
    FAKE_FWD_PORT=40003 FAKE_MODE=Enforcing run bats_run_one qci-fake "$TMP/e.bats" 40003
    [ "$status" -eq 0 ]
    [ "$(cat "$TMP/ran")" = 40003 ]
}

# --- avc-denials.sh fails closed (sol r84 finding 1) ------------------------

_avc_stub() {  # _avc_stub <ausearch-rc> <ausearch-output> [auditd-active=0]
    mkdir -p "$TMP/bin"
    printf '#!/bin/sh\nprintf "%%s\\n" "%s"\nexit %s\n' "$2" "$1" > "$TMP/bin/ausearch"
    printf '#!/bin/sh\nexit %s\n' "${3:-0}" > "$TMP/bin/systemctl"
    chmod +x "$TMP/bin/ausearch" "$TMP/bin/systemctl"
}

AVC="tests/integration/vm/probes/avc-denials.sh"

@test "avc-denials: ausearch missing is unusable, not clean" {
    mkdir -p "$TMP/bin"
    printf '#!/bin/sh\nexit 0\n' > "$TMP/bin/systemctl"; chmod +x "$TMP/bin/systemctl"
    # PATH is ONLY the stub dir, so no real ausearch can be found.
    run env PATH="$TMP/bin" "$BASH" "$REPO_ROOT/$AVC" qdistro_presentation_t
    [ "$status" -eq 3 ]
    [[ "$output" == *"AVC-UNUSABLE: ausearch not installed"* ]]
}

@test "avc-denials: ausearch error is unusable, not clean" {
    _avc_stub 10 "Error opening /var/log/audit/audit.log (Permission denied)"
    PATH="$TMP/bin:$PATH" run bash "$REPO_ROOT/$AVC" qdistro_presentation_t
    [ "$status" -eq 3 ]
    [[ "$output" == *"AVC-UNUSABLE: ausearch rc=10"* ]]
}

@test "avc-denials: inactive auditd is unusable" {
    _avc_stub 1 "<no matches>" 3
    PATH="$TMP/bin:$PATH" run bash "$REPO_ROOT/$AVC" qdistro_presentation_t
    [ "$status" -eq 3 ]
    [[ "$output" == *"auditd.service not active"* ]]
}

@test "avc-denials: no matches is clean; unrelated denial is clean" {
    _avc_stub 1 "<no matches>"
    PATH="$TMP/bin:$PATH" run bash "$REPO_ROOT/$AVC" qdistro_presentation_t
    [ "$status" -eq 0 ] && [ "$output" = AVC-CLEAN ]
    _avc_stub 0 "type=AVC msg=audit(1.2:3): avc:  denied  { read } for scontext=a tcontext=system_u:object_r:etc_t:s0 tclass=file"
    PATH="$TMP/bin:$PATH" run bash "$REPO_ROOT/$AVC" qdistro_presentation_t
    [ "$status" -eq 0 ] && [ "$output" = AVC-CLEAN ]
}

@test "avc-denials: a matching denial fails and is printed" {
    _avc_stub 0 "type=AVC msg=audit(1.2:3): avc:  denied  { watch } for scontext=c tcontext=system_u:object_r:qdistro_presentation_t:s0 tclass=file"
    PATH="$TMP/bin:$PATH" run bash "$REPO_ROOT/$AVC" qdistro_presentation_t
    [ "$status" -eq 1 ]
    [[ "$output" == *"denied  { watch }"*"qdistro_presentation_t"* ]]
}

@test "presentation-enforcing AVC case uses the fail-closed probe" {
    f="$REPO_ROOT/tests/integration/vm/presentation-enforcing.bats"
    grep -q 'avc-denials.sh' "$f"
    ! grep -q 'ausearch .*|| true' "$f"
}

# --- write-ahead registration around the enforcing clone (sol r85) ----------

_fake_clone_tools() {  # _fake_clone_tools <exit-rc> [print-vm=1]
    VM_TOOLS="$TMP/tools"; mkdir -p "$VM_TOOLS"
    cat > "$VM_TOOLS/clone-baseweed.sh" <<SH
#!/bin/bash
# Record what the write-ahead dir held WHILE the clone ran.
cat "$TMP/run/vm/provisioning.d/"*.wa > "$TMP/wa-during" 2>/dev/null || echo none > "$TMP/wa-during"
if [ "${2:-1}" = 1 ]; then echo "qci-bats-x-261005-000000-1-1"; echo "ssh_port=41234"; fi
exit $1
SH
    chmod +x "$VM_TOOLS/clone-baseweed.sh"
    RUN_GOLDEN_BATS="$TMP/golden.qcow2"; CREATED_VMS=()
    vm_list_by_prefix() { :; }
    reap_new_orphans() { echo "reap $*" >> "$TMP/reaped"; }
    kv() { :; }
    qci_enforcing_ssh() { return 0; }
}

@test "enforcing acquire registers a write-ahead marker while cloning, drops it once tracked" {
    _fake_clone_tools 0
    run acquire_enforcing_vm bats-x
    [ "$status" -eq 0 ]
    [ "${lines[-1]}" = "qci-bats-x-261005-000000-1-1 41234" ]
    grep -q "^prefix	qci-bats-x-$" "$TMP/wa-during"
    grep -q "^win_start	[0-9]" "$TMP/wa-during"
    [ -z "$(ls "$TMP/run/vm/provisioning.d/" 2>/dev/null)" ]
    grep -qx "qci-bats-x-261005-000000-1-1" "$TMP/run/vm/created-vms.txt"
}

@test "a failed enforcing clone reaps and drops the marker" {
    _fake_clone_tools 7 0
    run acquire_enforcing_vm bats-x
    [ "$status" -eq "$EXIT_VM_PROVISION" ]
    grep -q "^prefix	qci-bats-x-$" "$TMP/wa-during"
    grep -q "^reap qci-bats-x-" "$TMP/reaped"
    [ -z "$(ls "$TMP/run/vm/provisioning.d/" 2>/dev/null)" ]
}

@test "an interrupted enforcing worker leaves a marker the end-of-run sweep reaps" {
    _fake_clone_tools 0
    # Simulate a kill after the clone but before tracking: the marker the
    # acquire wrote is still there when finish_run sweeps.
    mkdir -p "$TMP/run/vm/provisioning.d"
    printf 'prefix\tqci-bats-x-\nbaseline\t\nwin_start\t%s\n' "$(date +%s)" \
        > "$TMP/run/vm/provisioning.d/worker-1.wa"
    run reap_writeahead_orphans
    [ "$status" -eq 0 ]
    grep -q "^reap qci-bats-x- " "$TMP/reaped"
    [ ! -e "$TMP/run/vm/provisioning.d/worker-1.wa" ]
}
