#!/usr/bin/env bats
#
# Host-only self-test of the qci gate-runner CONTRACT (no VM, no libvirt).
#
# These lock down the runner invariants the local-CI proposal (todo/ci/gates.md)
# depends on so local triage agents can act on a run without guessing:
#   - the stable exit-class table (0/10/20/30/35/40/50/60/70/80/90);
#   - usage / unknown-command dispatch return EXIT_USAGE (2);
#   - a headless gate (preflight/lint/selftest) writes a well-formed run dir
#     (manifest.txt + results.tsv + exit-code.txt) with the documented
#     exit classification, even when the underlying environment is incomplete.
#
# They drive the REAL qci runner with QCI_RUNS_DIR pointed at a temp dir. No
# gate here boots a VM: preflight only inspects tools/repos, lint/selftest are
# pre-VM, and we never invoke vm-smoke/gui/full.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    QCI="$REPO_ROOT/ci/bin/qci"
    RUNS="$(mktemp -d)"
    export QCI_RUNS_DIR="$RUNS"
}

teardown() {
    rm -rf "$RUNS"
}

latest_run_dir() {
    find "$RUNS" -mindepth 1 -maxdepth 1 -type d | sort | tail -1
}

# ---------------------------------------------------------------------------
# Usage / dispatch contract
# ---------------------------------------------------------------------------

@test "contract: no command prints usage and exits EXIT_USAGE (2)" {
    run "$QCI"
    [ "$status" -eq 2 ]
    [[ "$output" == *"Usage:"* ]]
}

@test "contract: --help exits EXIT_USAGE (2) and lists the proposal gates" {
    run "$QCI" --help
    [ "$status" -eq 2 ]
    [[ "$output" == *"qci preflight"* ]]
    [[ "$output" == *"qci host"* ]]
    [[ "$output" == *"qci vm-smoke"* ]]
    [[ "$output" == *"qci gui"* ]]
    [[ "$output" == *"qci full"* ]]
    [[ "$output" == *"qci selftest"* ]]
}

@test "contract: unknown command exits EXIT_USAGE (2)" {
    run "$QCI" not-a-real-gate
    [ "$status" -eq 2 ]
}

# ---------------------------------------------------------------------------
# Exit-class table contract (gates.md). Drives the real runner through a gate
# whose pass/fail is deterministic on any host and asserts the classification
# the runner records matches the documented table.
# ---------------------------------------------------------------------------

@test "contract: a passing headless gate records exit_class=pass and exit_code=0" {
    # lint is pre-VM and never hard-fails (shellcheck/bats are warn/skip), so it
    # is a deterministic 'pass' on any host.
    run "$QCI" lint
    [ "$status" -eq 0 ]
    local dir mf
    dir="$(latest_run_dir)"
    [ -n "$dir" ]
    mf="$dir/manifest.txt"
    [ -f "$mf" ]
    grep -q '^exit_code=0$' "$mf"
    grep -q '^exit_class=pass$' "$mf"
    [ "$(cat "$dir/exit-code.txt")" = "0" ]
}

@test "contract: every run writes a well-formed manifest + results.tsv header" {
    run "$QCI" lint
    [ "$status" -eq 0 ]
    local dir
    dir="$(latest_run_dir)"
    # Manifest carries the fields a triage agent reads first.
    grep -q '^run_id=' "$dir/manifest.txt"
    grep -q '^gate=lint$' "$dir/manifest.txt"
    grep -q '^started_utc=' "$dir/manifest.txt"
    grep -q '^finished_utc=' "$dir/manifest.txt"
    grep -q '^command=' "$dir/manifest.txt"
    # results.tsv has the stable 8-column header.
    head -1 "$dir/results.tsv" | grep -qx 'gate	subject	status	exit_code	exit_class	kind	log	notes'
    # Per-repo commit/dirty state is captured for the triage handoff.
    [ -f "$dir/repo-state.tsv" ]
    head -1 "$dir/repo-state.tsv" | grep -qx 'repo	branch	head	dirty_files	status_log'
}

@test "contract: results.tsv exit_class column is always a known class name" {
    run "$QCI" lint
    [ "$status" -eq 0 ]
    local dir
    dir="$(latest_run_dir)"
    # Skip header; every data row's exit_class (col 5) must be in the table.
    run awk -F'\t' 'NR>1 && NF>=5 {
        c=$5
        if (c !~ /^(pass|preflight|build|host|bats|vm_provision|vm_boot|service|gui|visual|runner)$/) {
            print "BAD_CLASS:" c; bad=1
        }
    } END { exit bad }' "$dir/results.tsv"
    [ "$status" -eq 0 ]
    [[ "$output" != *BAD_CLASS* ]]
}

@test "contract: preflight with a broken libvirt classifies as preflight (10), never silently passes" {
    # Shadow virsh with a stub whose `list` fails, so the required "libvirt
    # session" check misses. preflight must classify as EXIT_PREFLIGHT (10) — a
    # fail-CLOSED env gate — rather than returning 0. The rest of PATH (and the
    # runner's own shebang) stays intact.
    local stub
    stub="$(mktemp -d)"
    cat > "$stub/virsh" <<'SH'
#!/usr/bin/env bash
# dominfo/dumpxml succeed harmlessly; `list` (the libvirt-session probe) fails.
case "$*" in
    *list*) exit 1 ;;
    *) exit 0 ;;
esac
SH
    chmod +x "$stub/virsh"
    PATH="$stub:$PATH" run "$QCI" preflight
    rm -rf "$stub"
    [ "$status" -eq 10 ]
    local dir
    dir="$(latest_run_dir)"
    grep -q '^exit_class=preflight$' "$dir/manifest.txt"
    [ "$(cat "$dir/exit-code.txt")" = "10" ]
}

# ---------------------------------------------------------------------------
# selftest gate contract: the runner can self-test headlessly.
# ---------------------------------------------------------------------------

@test "contract: selftest gate runs headlessly and records a result" {
    # selftest runs the host-only qci bats suites (this one included) in an
    # isolated runs dir; it must finish 0 here and leave a selftest result.
    run "$QCI" selftest
    [ "$status" -eq 0 ]
    local dir
    dir="$(latest_run_dir)"
    grep -q '^gate=selftest$' "$dir/manifest.txt"
    grep -q '^exit_code=0$' "$dir/manifest.txt"
    # A selftest row exists (pass when bats present, skip when not).
    awk -F'\t' 'NR>1 && $1=="selftest"' "$dir/results.tsv" | grep -q .
}
