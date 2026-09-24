#!/usr/bin/env bats
#
# `qci <sub> -h|--help` must print usage and exit EXIT_USAGE (2) — the same
# code as top-level `qci --help` — WITHOUT creating a run dir, recording a
# result, or touching libvirt. Before the fix every subcommand called
# init_run first, and then either ran its gate (--help ignored, or taken as a
# bats file / triage run dir) or recorded the flag as an "unknown arg".
#
# Drives the REAL ci/bin/qci with QCI_RUNS_DIR in a temp dir and a stub
# `virsh` first on PATH that logs any call, so a regression cannot reach the
# host's libvirt and is caught by the "virsh never called" assertion.

# Every subcommand main() dispatches, one entry per case arm.
SUBCOMMANDS=(preflight lint selftest image registry-check release-manifest
    bootstrap-release-profile affected edit-guard replay host vm-smoke bats
    gui gui-admin full snapshot-daily mmnet cleanup report triage list-runs)

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    QCI="$REPO_ROOT/ci/bin/qci"
    RUNS="$(mktemp -d)"
    export QCI_RUNS_DIR="$RUNS"
    STUB="$(mktemp -d)"
    VIRSH_LOG="$STUB/virsh.calls"
    printf '#!/bin/sh\necho "$*" >> "%s"\nexit 1\n' "$VIRSH_LOG" > "$STUB/virsh"
    chmod +x "$STUB/virsh"
    export PATH="$STUB:$PATH"
}

teardown() {
    rm -rf "$RUNS" "$STUB"
}

run_count() {
    find "$RUNS" -mindepth 1 -maxdepth 1 | wc -l
}

# Assert one help invocation: usage printed, exit 2, no run dir, no virsh.
assert_help() {
    run timeout 60 "$QCI" "$@"
    if [ "$status" -ne 2 ]; then
        echo "qci $*: status=$status (want 2)"; echo "$output"; return 1
    fi
    if [[ "$output" != *"Usage:"* ]] || [[ "$output" != *"qci list-runs"* ]]; then
        echo "qci $*: usage text missing"; echo "$output"; return 1
    fi
    if [[ "$output" == *"unknown command"* ]]; then
        echo "qci $*: dispatched as unknown command"; echo "$output"; return 1
    fi
    if [ "$(run_count)" -ne 0 ]; then
        echo "qci $*: created a run dir:"; ls "$RUNS"; return 1
    fi
    if [ -e "$VIRSH_LOG" ]; then
        echo "qci $*: called virsh:"; cat "$VIRSH_LOG"; return 1
    fi
}

@test "subcommand --help: every subcommand prints usage, exits 2, no run dir" {
    local sub
    for sub in "${SUBCOMMANDS[@]}"; do
        assert_help "$sub" --help
    done
}

@test "subcommand -h: every subcommand prints usage, exits 2, no run dir" {
    local sub
    for sub in "${SUBCOMMANDS[@]}"; do
        assert_help "$sub" -h
    done
}

@test "subcommand --help after other args still means help (gui/bats/image/full/replay)" {
    assert_help gui --vm some-vm --scenario x.md --help
    assert_help bats --file /dev/null --help
    assert_help image --no-boot --help
    assert_help full --keep-on-fail --help
    assert_help replay some-scenario some-vm --help
    assert_help triage --latest --help
    assert_help affected --changed-from HEAD --help
}

@test "qci help <sub> prints usage, exits 2, no run dir" {
    assert_help help gui
    assert_help help full
}

@test "every command in the Usage: block is a dispatched subcommand" {
    run "$QCI" --help
    local cmds
    cmds=$(printf '%s\n' "$output" | sed -n 's/^  qci \([a-z-]*\).*/\1/p' | sort -u)
    [ -n "$cmds" ]
    local sub
    for sub in $cmds; do
        assert_help "$sub" --help
    done
    # ...and the test's own list matches the Usage: block exactly.
    [ "$cmds" = "$(printf '%s\n' "${SUBCOMMANDS[@]}" | sort -u)" ]
}

@test "unknown command exits 2 with usage and creates no run dir" {
    run "$QCI" not-a-real-gate --help
    [ "$status" -eq 2 ]
    [[ "$output" == *"unknown command: not-a-real-gate"* ]]
    [[ "$output" == *"Usage:"* ]]
    [ "$(run_count)" -eq 0 ]
}

@test "--help after -- is an operand, not a help request (affected path)" {
    # affected without --run only maps paths to gates (no VM); the `--`
    # terminator must pass --help through as a path, so a run dir IS made.
    run timeout 60 "$QCI" affected -- --help
    [[ "$output" != *"Usage:"* ]]
    [ "$(run_count)" -eq 1 ]
    [ ! -e "$VIRSH_LOG" ]
}
