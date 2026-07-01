#!/usr/bin/env bash
# qci module: selftest gate (runner self-test)
# Extracted verbatim from bin/qci. SOURCED by bin/qci into the single
# CI-runner process (shared RDIR/CREATED_VMS/golden state/traps); it is
# NOT executed standalone. See ci/AGENTS.md for the module map.
# shellcheck shell=bash

# ---------------------------------------------------------------------------
# Self-test gate: run the host-only qci runner unit tests headlessly (NO VM).
#
# These bats files under tests/integration/qci/ drive the real qci runner with
# QCI_RUNS_DIR pointed at a throwaway dir and stub virsh/bats on PATH where a
# dispatch path is asserted. They lock down the gate-runner CONTRACT that the
# CI proposal hinges on (gates.md): the stable exit-class table, usage/unknown
# dispatch, headless gate execution producing a manifest + results.tsv with the
# documented classification, and the changed-path -> gate selection / replay /
# offline plumbing. This is the smoke test you can run on any host with no
# libvirt: missing bats => skip+warn (matches the lint convention). EXIT_BATS
# on failure.
# ---------------------------------------------------------------------------
gate_selftest() {
    qci_assert_run_dir || return $?
    local log_path="$RDIR/selftest/qci-selftest.log" rc=$EXIT_OK
    mkdir -p "$(dirname "$log_path")"
    : > "$log_path"
    # Recursion guard: the bats suite this gate runs itself invokes `qci
    # selftest` (to assert the gate dispatches headlessly). When that nested
    # call sees QCI_IN_SELFTEST=1, it records a no-op pass instead of re-running
    # the suite — otherwise selftest would recurse forever.
    if [ "${QCI_IN_SELFTEST:-0}" = 1 ]; then
        record_result selftest qci-bats skip 0 pass selftest "$log_path" "nested selftest invocation; suite already running (recursion guard)"
        return "$EXIT_OK"
    fi

    # Entrypoint inventory check: verify every security-sensitive entry point
    # in ci/entrypoint-inventory.tsv has a mapped test file that exists on
    # disk. This is the mechanical guard that catches the P0 class of gap
    # where a security-sensitive CLI/service ships with no test at all.
    # The check is lightweight (a TSV read + file existence check) — it needs
    # only bash and the TSV, NOT bats — so it runs BEFORE the bats early-returns
    # to guarantee it always executes and always records a pass/fail/skip row
    # regardless of bats availability.
    local ep_log ep_rc ep_script
    ep_log="$RDIR/selftest/entrypoint-inventory.log"
    ep_script="$QCI_BIN_DIR/check-entrypoint-inventory.sh"
    if [ -x "$ep_script" ]; then
        {
            echo "## entrypoint-inventory check"
            echo "inventory: $QDISTRO_REPO/ci/entrypoint-inventory.tsv"
            echo
        } > "$ep_log"
        "$ep_script" "$QDISTRO_REPO" >> "$ep_log" 2>&1
        ep_rc=$?
        if [ "$ep_rc" -eq 0 ]; then
            record_result selftest entrypoint-inventory pass 0 pass selftest \
                "$ep_log" "all entry-point->test mappings satisfied"
        else
            rc=$EXIT_BATS
            record_result selftest entrypoint-inventory fail "$EXIT_BATS" bats selftest \
                "$ep_log" "one or more entry-point test files are missing"
        fi
        log "selftest: entrypoint-inventory check exit=$ep_rc"
    else
        record_skip selftest entrypoint-inventory selftest \
            "ci/bin/check-entrypoint-inventory.sh not found or not executable"
        log "selftest: entrypoint-inventory check script not found (skip)"
    fi

    if ! command -v bats >/dev/null 2>&1; then
        record_skip selftest qci-bats selftest "bats not installed; skipping qci runner self-test"
        log "selftest: bats not installed (skip)"
        log "selftest: exit $rc"
        return "$rc"
    fi
    local sf bats_files=()
    while IFS= read -r sf; do [ -n "$sf" ] && bats_files+=("$sf"); done \
        < <(find "$QDISTRO_REPO/tests/integration/qci" -maxdepth 1 -name '*.bats' -type f | sort)
    if [ "${#bats_files[@]}" -eq 0 ]; then
        record_skip selftest qci-bats selftest "no tests/integration/qci/*.bats present"
        log "selftest: no qci self-test bats files found (skip)"
        log "selftest: exit $rc"
        return "$rc"
    fi
    {
        echo "## qci runner self-test (host-only, no VM)"
        echo "bats $(bats --version 2>/dev/null)"
        echo "files: ${#bats_files[@]}"
        echo
    } >> "$log_path"
    log "selftest: running ${#bats_files[@]} qci self-test bats files (no VM)"
    # Isolate the self-test's own runs from this gate's artifact dir so the
    # nested qci invocations cannot pollute $RDIR (or recurse into runs/).
    local sf_runs
    sf_runs=$(mktemp -d "${TMPDIR:-/tmp}/qci-selftest.XXXXXX")
    # Env scrub for the nested bats: this gate runs INSIDE the already-bootstrapped
    # qci process, so every WORKSPACE and <NAME>_REPO that bootstrap.sh exports is
    # in the environment. env-repo-exports.bats asserts "no WORKSPACE => <NAME>_REPO
    # is [unset]", which cannot hold if the vars are inherited. Strip WORKSPACE and
    # each PROJECTS-derived <NAME>_REPO so the nested bats see a clean slate; the
    # qci runner rediscovers both from bin/qci's own location, so this does not
    # affect the self-test's real qci invocations. Derived from PROJECTS so it can
    # never rot when a project is added.
    local -a sf_scrub=(-u WORKSPACE)
    local _sf_proj _sf_var
    for _sf_proj in "${PROJECTS[@]}"; do
        _sf_var=$(printf '%s' "$_sf_proj" | tr '[:lower:]-' '[:upper:]_')_REPO
        sf_scrub+=(-u "$_sf_var")
    done
    unset _sf_proj _sf_var
    # Run the suite from the repo root: the contract bats resolve some inputs by
    # repo-relative path (e.g. `qci replay tests/integration/vm/x.bats`), so the
    # suite assumes cwd == the qdistro checkout. The bats files are absolute
    # (from `find $QDISTRO_REPO/...`), so the cd doesn't affect discovery. This
    # makes selftest cwd-independent — it passes whether qci was invoked via
    # `just` (cwd=ci/), from the repo root, or from anywhere else.
    if ( cd "$QDISTRO_REPO" && QCI_IN_SELFTEST=1 QCI_RUNS_DIR="$sf_runs" env "${sf_scrub[@]}" bats "${bats_files[@]}" ) >> "$log_path" 2>&1; then
        record_result selftest qci-bats pass 0 pass selftest "$log_path" "${#bats_files[@]} qci self-test files passed"
    else
        rc=$EXIT_BATS
        record_result selftest qci-bats fail "$EXIT_BATS" bats selftest "$log_path" "qci runner self-test failed"
    fi
    rm -rf "$sf_runs" 2>/dev/null || true

    log "selftest: exit $rc"
    return "$rc"
}
