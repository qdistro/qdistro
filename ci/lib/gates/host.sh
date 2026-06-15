#!/usr/bin/env bash
# qci module: host gate (pytest/ruff/mypy/coverage/builds)
# Extracted verbatim from bin/qci. SOURCED by bin/qci into the single
# CI-runner process (shared RDIR/CREATED_VMS/golden state/traps); it is
# NOT executed standalone. See ci/AGENTS.md for the module map.
# shellcheck shell=bash

# ---------------------------------------------------------------------------
# Shared host pytest-runner snippet generator (DRY).
#
# Every Python repo's host pytest step is emitted by THIS one function, so the
# batching/segfault-isolation, env-pinning, and coverage logic live in a single
# place instead of being re-hand-rolled per repo. It prints a bash snippet that
# run_logged executes (via `bash -lc` after `cd $workdir`); the snippet always
# `exit`s with rc=0 iff every pytest invocation it made returned 0.
#
# It is deliberately a thin, faithful generator: each caller passes the exact
# parameters that reproduce that repo's PRE-EXISTING behaviour byte-for-byte
# (same file discovery, same batching, same env, same flags). It introduces NO
# behaviour change — see the per-repo BEFORE/AFTER trace in the commit message.
#
# Parameters (named, via local vars set by the caller-facing wrapper):
#   _hp_discover : how test files are found. One of:
#                    "all"            -> single pytest process over _hp_args
#                                        (NO per-file loop; passes _hp_args
#                                        straight to pytest, so pyproject
#                                        testpaths apply when _hp_args is empty)
#                    "glob:<pattern>" -> shell glob (non-recursive, unsorted),
#                                        guarded so an empty glob is a no-op
#                    "find:<expr>"    -> `find <expr> | sort` (recursive, sorted)
#   _hp_batch    : files per pytest process. 1 = strict per-file; N = N-per-batch
#                  (ignored for the "all" discover mode)
#   _hp_env      : env-var prefix string prepended to each `python3 -m pytest`
#                  (e.g. 'PYTEST_QT_API=pyqt6 QT_API=pyqt6 ...'); may be empty
#   _hp_args     : extra args appended to each pytest invocation (e.g. coverage
#                  flags, or for the "all" mode the test dir/selector); may be
#                  empty
#   _hp_pre      : optional shell prologue emitted before the loop (e.g. the
#                  qdistro coverage-flag setup / stale-artifact cleanup)
#   _hp_post     : optional shell epilogue emitted after the loop, before exit
#                  (e.g. the qdistro coverage-summary printer)
#
# All values are interpolated literally into the snippet (callers control them;
# they are not attacker-influenced), matching how host_ruff_cmd/host_mypy_cmd
# already build their snippets.
# ---------------------------------------------------------------------------
host_pytest_cmd() {
    local _hp_discover=$1 _hp_batch=${2:-1} _hp_env=${3:-} _hp_args=${4:-} _hp_pre=${5:-} _hp_post=${6:-}
    local _pytest="${_hp_env:+$_hp_env }python3 -m pytest"

    printf 'rc=0\n'
    [ -n "$_hp_pre" ] && printf '%s\n' "$_hp_pre"

    case "$_hp_discover" in
        all)
            # Single pytest process. _hp_args carries any dir/selector; when it
            # is empty, pyproject testpaths apply (bare `python3 -m pytest`).
            printf '%s%s || rc=1\n' "$_pytest" "${_hp_args:+ $_hp_args}"
            ;;
        glob:*)
            # Non-recursive shell glob, unsorted, empty-glob-safe.
            printf 'for t in %s; do\n' "${_hp_discover#glob:}"
            printf '    [ -e "$t" ] || continue\n'
            printf '    %s%s "$t" || rc=1\n' "$_pytest" "${_hp_args:+ $_hp_args}"
            printf 'done\n'
            ;;
        find:*)
            # Recursive find, sorted, batched (_hp_batch files per process).
            printf 'mapfile -t _f < <(find %s | sort)\n' "${_hp_discover#find:}"
            printf 'for ((_i=0; _i<${#_f[@]}; _i+=%s)); do\n' "$_hp_batch"
            printf '    %s%s "${_f[@]:_i:%s}" || rc=1\n' "$_pytest" "${_hp_args:+ $_hp_args}" "$_hp_batch"
            printf 'done\n'
            ;;
    esac

    [ -n "$_hp_post" ] && printf '%s\n' "$_hp_post"
    printf 'exit $rc\n'
}

# ---------------------------------------------------------------------------
# Blocking lint/type targets for the host gate.
#
# ruff (shared profile in ci/ruff-shared.toml) runs over the qdistro Python
# source dirs + each sibling repo's package + tests dirs. mypy (ci/mypy.ini)
# runs over the narrow security-sensitive subset (cli + browser_bridge; broker
# is a documented follow-up ratchet — see ci/mypy.ini).
#
# Both are calibrated GREEN on current main (see the profiles for the
# before/after counts). A MISSING tool => SKIP (exit 0 with a SKIP line),
# matching the optional-tooling convention used by gate_lint's shellcheck/bats.
# ---------------------------------------------------------------------------

# ruff target list (absolute paths, one per line). qdistro source dirs are a
# subset of pyproject's pythonpath that actually ships Python; sibling repos
# contribute <repo>/<pkg> + <repo>/tests.
host_ruff_targets() {
    local d
    for d in broker browser_bridge cli tui qsu polkit user_relay pwd print \
             phone recall browser_daemons daemons snapshots sdk admin_app \
             session_manager workflow templates plugins scripts ci deploy \
             tier4-vm tier5b-vm; do
        [ -d "$QDISTRO_REPO/$d" ] && printf '%s\n' "$QDISTRO_REPO/$d"
    done
    local repo
    for repo in qdbrowser qnotebook qterminator qdgreeter qdlocker qfileman; do
        [ -d "$WORKSPACE/$repo/$repo" ] && printf '%s\n' "$WORKSPACE/$repo/$repo"
        [ -d "$WORKSPACE/$repo/tests" ] && printf '%s\n' "$WORKSPACE/$repo/tests"
    done
}

# mypy target list. NARROW by design: only the cleanest security-sensitive
# dirs. broker/ joined the gate once its 22 Optional/None-narrowing +
# assignment-type errors were fixed at the source (02/S7).
host_mypy_targets() {
    local d
    for d in cli browser_bridge broker; do
        [ -d "$QDISTRO_REPO/$d" ] && printf '%s\n' "$QDISTRO_REPO/$d"
    done
}

# Emit the shell snippet run_logged executes for the ruff step. Skips (exit 0)
# when ruff is not on PATH so optional tooling never reddens the gate (gate_host
# also records a visible SKIP row in that case — see the preflight tool check).
# Target words are shell-quoted (printf %q) into the snippet so a path with a
# space/glob char can't word-split or expand; the config path is likewise quoted.
host_ruff_cmd() {
    local cfg targets t
    cfg="$QCI_DIR/ruff-shared.toml"
    targets=""
    while IFS= read -r t; do
        [ -n "$t" ] && targets+=" $(printf '%q' "$t")"
    done < <(host_ruff_targets)
    cat <<EOF
if ! command -v ruff >/dev/null 2>&1; then
    echo "SKIP: ruff not installed; skipping blocking lint"
    exit 0
fi
ruff --version
echo "config: $(printf '%q' "$cfg")"
ruff check --config $(printf '%q' "$cfg")$targets
EOF
}

# Emit the shell snippet for the mypy step. Skips (exit 0) when mypy absent.
# Targets + config are shell-quoted into the snippet (see host_ruff_cmd).
host_mypy_cmd() {
    local cfg targets t
    cfg="$QCI_DIR/mypy.ini"
    targets=""
    while IFS= read -r t; do
        [ -n "$t" ] && targets+=" $(printf '%q' "$t")"
    done < <(host_mypy_targets)
    cat <<EOF
if ! command -v mypy >/dev/null 2>&1; then
    echo "SKIP: mypy not installed; skipping blocking type check"
    exit 0
fi
mypy --version
echo "config: $(printf '%q' "$cfg")"
mypy --config-file $(printf '%q' "$cfg")$targets
EOF
}

# Per-project coverage FLOOR enforcement (taxonomy-aware; see
# ci/coverage-floors.tsv). Looks up the project's floor FIRST, then reads the
# coverage JSON artifact, and:
#   - no TSV row for project  -> visible skip (no floor configured)
#   - malformed floor in TSV   -> FAIL the gate (a positive-floor config typo
#                                 must never silently pass)
#   - floor == 0  -> report-only: pass when JSON present, visible skip when the
#                    JSON is missing (never blocks)
#   - floor  > 0  -> FAIL CLOSED: a missing/unparseable JSON FAILS the gate
#                    (EXIT_HOST); measured < floor FAILS; measured >= floor pass
# GATE INTEGRITY: a positive floor never "passes by absence" — if we cannot
# prove coverage met the floor, the gate goes red. Always emits a
# COVERAGE-SUMMARY line into the step log.
coverage_floor_check() {
    local project=$1 json=$2 rc=$EXIT_OK
    local floors_tsv="$QCI_DIR/coverage-floors.tsv"
    local log_path="$RDIR/host/$(safe_name "$project-coverage-floor").log"

    # 1. Look up the floor FIRST (before touching the JSON) so a missing
    #    artifact under a POSITIVE floor can fail closed. have_row distinguishes
    #    "no row at all" from an explicit 0 in the file.
    local floor note have_row
    floor=$(awk -F'\t' -v p="$project" '!/^#/ && $1==p {print $2; found=1; exit} END{exit found?0:1}' "$floors_tsv" 2>/dev/null)
    have_row=$?
    note=$(awk -F'\t' -v p="$project" '!/^#/ && $1==p {print $3; exit}' "$floors_tsv" 2>/dev/null)
    if [ "$have_row" -ne 0 ]; then
        # No row at all: distinct from an explicit 0 — surface as a visible skip
        # with a note rather than silently treating it as report-only.
        record_skip host "$project-coverage-floor" coverage "no coverage-floor row for $project in $(rel_path "$floors_tsv"); not enforced"
        return 0
    fi
    # 2. VALIDATE the floor. A positive-floor typo (80.5, 8O, 'eighty', a stray
    #    tab shifting fields) must fail the gate, never fall through to an
    #    accidental PASS via an errored numeric comparison. Empty cell == 0.
    [ -n "$floor" ] || floor=0
    case "$floor" in
        ''|*[!0-9]*)
            { printf 'COVERAGE-FLOOR: %s INVALID floor=%s in %s (must be a non-negative integer)\n' "$project" "$floor" "$(rel_path "$floors_tsv")"; } > "$log_path"
            log "coverage-floor: $project INVALID floor='$floor' (non-integer)"
            record_result host "$project-coverage-floor" fail "$EXIT_HOST" "$(exit_class_name "$EXIT_HOST")" coverage "$log_path" "malformed floor='$floor' in coverage-floors.tsv (non-integer)"
            return "$EXIT_HOST"
            ;;
    esac

    # 3. With the floor known and valid, handle a missing/unreadable JSON.
    if [ ! -f "$json" ] || [ ! -r "$json" ]; then
        if [ "$floor" -eq 0 ]; then
            # Report-only floor + no data: harmless skip.
            record_skip host "$project-coverage-floor" coverage "no coverage JSON ($project); floor=0 report-only"
            return 0
        fi
        # POSITIVE floor + no data: FAIL CLOSED. We cannot prove the floor was
        # met, so the gate must not stay green.
        { printf 'COVERAGE-FLOOR: %s MISSING coverage JSON (%s); floor=%s%% -> FAIL CLOSED\n' "$project" "$json" "$floor"; } > "$log_path"
        log "coverage-floor: $project MISSING coverage JSON; floor=$floor% -> FAIL CLOSED"
        record_result host "$project-coverage-floor" fail "$EXIT_HOST" "$(exit_class_name "$EXIT_HOST")" coverage "$log_path" "coverage JSON missing/unreadable but floor=$floor% (>0): failing closed"
        return "$EXIT_HOST"
    fi

    # 4. Measure. Prefer a numeric round() of percent_covered (robust to a
    #    nonzero [tool.coverage.report] precision making the *_display value a
    #    non-integer string like "88.38"); fall back to the display string, then
    #    to the istanbul (vitest coverage-final.json, which has NO 'totals' key)
    #    per-file 's' statement maps. The JSON path is passed as argv (NOT
    #    string-interpolated into the python source).
    local pct
    pct=$(python3 -c "
import json, sys
path = sys.argv[1]
d = json.load(open(path))
try:
    # KeyError here (not .get) is what triggers the istanbul fallback: an
    # istanbul coverage-final.json has no top-level 'totals' key.
    tot = d['totals']
    try:
        print(round(float(tot['percent_covered'])))
    except (KeyError, TypeError, ValueError):
        disp = tot.get('percent_covered_display')
        print(round(float(disp)) if disp not in (None, '') else 0)
except (KeyError, TypeError):
    # istanbul/vitest: compute line % from the per-file 's' statement maps.
    total = covered = 0
    for f, v in d.items():
        s = v.get('s', {}) if isinstance(v, dict) else {}
        total += len(s)
        covered += sum(1 for n in s.values() if n > 0)
    print(round(100 * covered / total) if total else 0)
" "$json" 2>/dev/null)
    [ -n "$pct" ] || pct='?'
    {
        printf 'COVERAGE-SUMMARY: %s measured=%s%% floor=%s%%\n' "$project" "$pct" "$floor"
        printf 'floor-note: %s\n' "${note:-}"
    } > "$log_path"
    log "coverage-floor: $project measured=$pct% floor=$floor%"
    if [ "$floor" -eq 0 ]; then
        record_result host "$project-coverage-floor" pass 0 pass coverage "$log_path" "measured=$pct% floor=0% (report-only)"
        return 0
    fi
    # Positive floor: block when measured is not a clean number (parse failed →
    # fail closed) or is strictly below the floor.
    if [ "$pct" = "?" ] || ! [ "$pct" -ge 0 ] 2>/dev/null; then
        record_result host "$project-coverage-floor" fail "$EXIT_HOST" "$(exit_class_name "$EXIT_HOST")" coverage "$log_path" "could not parse coverage; floor=$floor% (failing closed)"
        return "$EXIT_HOST"
    fi
    if [ "$pct" -lt "$floor" ]; then
        record_result host "$project-coverage-floor" fail "$EXIT_HOST" "$(exit_class_name "$EXIT_HOST")" coverage "$log_path" "measured=$pct% BELOW floor=$floor%"
        rc=$EXIT_HOST
    else
        record_result host "$project-coverage-floor" pass 0 pass coverage "$log_path" "measured=$pct% >= floor=$floor%"
    fi
    return "$rc"
}

gate_host() {
    qci_assert_run_dir || return $?
    qci_assert_repo host || return $?
    local rc=$EXIT_OK c step_rc
    # Bound EVERY host step with a hard wall-clock timeout (run_logged honours
    # RUN_STEP_TIMEOUT) so a single wedged suite can never stall the gate again
    # — the qterminator QPrinter()/CUPS hang sat forever with no timeout and
    # blocked the whole `full` pipeline from reaching the parallel VM gates.
    # Overridable via QCI_HOST_STEP_TIMEOUT (seconds); reset before return so it
    # does not leak into later gates in the same process.
    local _saved_step_to="${RUN_STEP_TIMEOUT:-0}"
    RUN_STEP_TIMEOUT="${QCI_HOST_STEP_TIMEOUT:-600}"
    # CI-integrity guard, FIRST: flag agent edits to protected paths (tests/**,
    # ci/prompts/**, selinux/**) before spending any build/test time. This is
    # the CI-wired invocation — no explicit ref/paths, so it diffs against the
    # merge-base with the integration branch (QCI_BASE_REF) and therefore
    # catches COMMITTED protected edits, not just the (empty-at-CI-time)
    # working-tree diff. Sanction with QCI_ALLOW_TEST_EDITS=1 for genuine
    # test/CI maintenance tasks. Fail-safe: an unresolvable base FAILS the gate.
    gate_edit_guard "" 0; step_rc=$?
    [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc

    # Self-test the qci runner itself (host-only, no VM) before spending build
    # time. If the gate-runner contract (exit classes, dispatch, manifest) is
    # broken, every downstream result is untrustworthy — fail fast here.
    gate_selftest; step_rc=$?
    [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc

    # BLOCKING static lint + narrow type check, before the (slow) test suites so
    # a lint break is reported fast. Both use the shared profiles in ci/ and are
    # calibrated green on current main. Run from $QDISTRO_REPO (the actual
    # qdistro checkout, NOT a name-hardcoded $WORKSPACE/qdistro — so a worktree
    # named anything else still works) so the per-file-ignore globs resolve
    # relative to that dir (see ruff-shared.toml); the targets are absolute
    # paths spanning qdistro + sibling repos.
    #
    # A MISSING tool must NOT redden the gate, but it also must NOT masquerade as
    # a clean lint run: probe for the tool FIRST (in the same bash -lc login
    # shell run_logged uses, so a login-PATH-only install is seen consistently)
    # and record an explicit SKIP row when absent, instead of the run_logged
    # step's in-snippet `exit 0` landing as an indistinguishable PASS.
    if bash -lc "command -v ruff >/dev/null 2>&1"; then
        run_logged host qdistro-ruff "$EXIT_HOST" lint "$QDISTRO_REPO" "$(host_ruff_cmd)" "shared ruff profile (blocking)"; step_rc=$?
        [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc
    else
        record_skip host qdistro-ruff lint "ruff not installed; blocking lint skipped"
    fi
    if bash -lc "command -v mypy >/dev/null 2>&1"; then
        run_logged host qdistro-mypy "$EXIT_HOST" lint "$QDISTRO_REPO" "$(host_mypy_cmd)" "narrow mypy: cli+browser_bridge (blocking)"; step_rc=$?
        [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc
    else
        record_skip host qdistro-mypy lint "mypy not installed; blocking type check skipped"
    fi

    # Pin the Qt binding to PyQt6 so a CI image that also has PySide6
    # installed (qdgreeter uses it) can't let pytest-qt load PySide6 first
    # and shadow PyQt6, which would silently SKIP the ~150 admin-app Qt
    # tests. QDISTRO_REQUIRE_PYQT6=1 makes the conftest turn that shadow
    # into a hard failure instead of a quiet skip.
    # Run the unit tests in file batches (~30 files each) rather than one
    # giant process. A single native segfault (e.g. a Qt teardown crash) then
    # fails only that batch instead of aborting the whole ~2300-test suite,
    # and the crash is attributable to a small file set. rc=1 if any batch fails.
    #
    # Coverage (REPORT-ONLY): when pytest-cov is available, collect coverage
    # across the run and emit a per-module report + uncovered-file list as a
    # run artifact. No floor / no pass-rate gate — the coverage number informs
    # but never gates the pipeline. Flags are passed at invocation time only;
    # individual product repos' pyproject.toml are NOT edited.
    # Prologue: clear any stale coverage artifact from a previous qci run in
    # this checkout BEFORE the run, regardless of pytest-cov availability.
    # Otherwise a leftover .coverage-report.json (e.g. from an earlier run where
    # pytest-cov was installed) could be picked up by the floor check below and
    # silently satisfy a positive floor even though THIS run produced no
    # coverage. Scope the reported percentage to this run only; --cov-append
    # still accumulates INTER-batch within a single run.
    local _qd_pre _qd_post
    _qd_pre='# ALWAYS clear any stale coverage artifact (see comment above).
rm -f .coverage .coverage-report.json
if python3 -c "import pytest_cov" 2>/dev/null; then
    _COV_FLAGS="--cov=. --cov-report=term-missing --cov-report=json:.coverage-report.json --cov-append"
else
    _COV_FLAGS=""
fi'
    _qd_post='if [ -n "$_COV_FLAGS" ] && [ -f .coverage-report.json ]; then
    python3 -c "
import json, sys
try:
    d = json.load(open(\".coverage-report.json\"))
    tot = d.get(\"totals\", {})
    pct = tot.get(\"percent_covered_display\", \"?\")
    n_files = len(d.get(\"files\", {}))
    missing = [f for f, v in d.get(\"files\", {}).items() if v.get(\"summary\", {}).get(\"missing_lines\", 0) > 0]
    print(f\"COVERAGE-SUMMARY: {pct}% covered across {n_files} files\")
    print(f\"UNCOVERED-FILES ({len(missing)} with missing lines):\")
    for f in sorted(missing): print(f\"  {f}\")
except Exception as e:
    print(f\"coverage report parse error: {e}\", file=sys.stderr)
" || true
fi'
    # batch=30 recursive find (segfault isolation), PyQt6 env pinned, coverage
    # flags spliced before the file list (unquoted, intentional word-splitting).
    c=$(host_pytest_cmd \
        'find:tests/unit -name "test_*.py"' 30 \
        'PYTEST_QT_API=pyqt6 QT_API=pyqt6 QDISTRO_REQUIRE_PYQT6=1' \
        '${_COV_FLAGS}' "$_qd_pre" "$_qd_post")
    run_logged host qdistro-pytest "$EXIT_HOST" pytest "$QDISTRO_REPO" "$c" "qdistro unit tests"; step_rc=$?
    [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc

    # Publish the coverage report as a run artifact WHEN AVAILABLE (report-only
    # artifact row). The artifact copy is conditional on the JSON existing...
    if [ -f "$QDISTRO_REPO/.coverage-report.json" ]; then
        cp "$QDISTRO_REPO/.coverage-report.json" "$RDIR/host/qdistro-coverage.json" 2>/dev/null || true
        record_result host qdistro-coverage pass 0 pass coverage \
            "$RDIR/host/qdistro-pytest.log" "coverage report (report-only artifact)"
    fi
    # ...but the per-project FLOOR check is ALWAYS run, OUTSIDE that `if`. If
    # pytest-cov was absent / coverage write failed / the copy failed, the JSON
    # is missing and coverage_floor_check itself decides: report-only (floor=0)
    # skips, but a POSITIVE floor FAILS CLOSED rather than leaving the gate green
    # by absence. Report-only (floor=0) today; BLOCKS once a positive floor is
    # calibrated. (ci/coverage-floors.tsv.)
    coverage_floor_check qdistro "$RDIR/host/qdistro-coverage.json"; step_rc=$?
    [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc

    c="rm -rf build-qci && meson setup build-qci --prefix=/usr && meson compile -C build-qci && meson test -C build-qci --print-errorlogs"
    run_logged host qdwin-meson "$EXIT_BUILD" build "$WORKSPACE/qdwin" "$c" "qdwin build and meson tests"; step_rc=$?
    [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc

    c="find tests -name '*.sh' -print0 | xargs -0 -r -n1 bash -n"
    run_logged host qdwin-shell-syntax "$EXIT_HOST" syntax "$WORKSPACE/qdwin" "$c" "syntax check qdwin test helpers"; step_rc=$?
    [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc

    # Headless gate for the SHIPPED (production-profile) vendored
    # libweston: builds it and asserts the four soft-linked
    # weston_desktop_xdg_popup_* helper symbols are exported + the qdistro
    # staging script lays a complete tree. This is the no-VM proof that
    # the packaging decision ships a usable patched library. Backends that
    # need devel packages absent on the runner can be toggled off via
    # QDWIN_LW_* without affecting the symbol assertion (the helpers live
    # in the always-built libweston-desktop core).
    c="bash libweston-vendored/run-production-symbols-test.sh"
    run_logged host qdwin-vendored-libweston-symbols "$EXIT_BUILD" build "$WORKSPACE/qdwin" "$c" "vendored libweston production build exports popup helper symbols"; step_rc=$?
    [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc

    c="export PKG_CONFIG_PATH=\"$WORKSPACE/qdwin/build-qci/meson-uninstalled:\${PKG_CONFIG_PATH:-}\" && rm -rf build-qci && meson setup build-qci --prefix=/usr && meson compile -C build-qci && scripts/ci-local.sh --no-int"
    run_logged host qdshell-local "$EXIT_HOST" qml "$WORKSPACE/qdshell" "$c" "qdshell build, qmltest, jstest (node), lint, format check"; step_rc=$?
    [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc

    # qdbrowser: strict per-file via the shared runner (batch=1 over the
    # non-recursive tests/test_*.py glob) to bound QtWebEngine native residue.
    run_logged host qdbrowser-pytest "$EXIT_HOST" pytest "$WORKSPACE/qdbrowser" "$(host_pytest_cmd 'glob:tests/test_*.py' 1)" "qdbrowser pytest per file to reduce QtWebEngine residue"; step_rc=$?
    [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc
    # qdgreeter/qdlocker/qfileman/qnotebook/qterminator: single-process run via
    # the shared runner ("all" mode). The selector arg reproduces each repo's
    # prior bare invocation (explicit dir, or empty => pyproject testpaths).
    run_logged host qdgreeter-pytest "$EXIT_HOST" pytest "$WORKSPACE/qdgreeter" "$(host_pytest_cmd all 0 '' tests)" ""; step_rc=$?
    [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc
    run_logged host qdlocker-pytest "$EXIT_HOST" pytest "$WORKSPACE/qdlocker" "$(host_pytest_cmd all 0 '' tests/unit)" ""; step_rc=$?
    [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc
    run_logged host qfileman-pytest "$EXIT_HOST" pytest "$WORKSPACE/qfileman" "$(host_pytest_cmd all)" ""; step_rc=$?
    [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc
    run_logged host qnotebook-pytest "$EXIT_HOST" pytest "$WORKSPACE/qnotebook" "$(host_pytest_cmd all)" ""; step_rc=$?
    [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc
    # qterminator: exclude the printer-coupled print_terminal tests on the host.
    # They construct QPrinter(QPrinter.PrinterMode.HighResolution), which resolves
    # the host's DEFAULT printer and blocks indefinitely when that printer is an
    # offline/unreachable network device (this workstation's Phaser-3020) — with
    # no per-suite timeout that wedged the entire `full` pipeline. Qt ignores
    # CUPS_SERVER for that path, so there is NO host-side env fix; these tests
    # belong in a VM lane (clean image => no printer => no hang). Recorded below
    # as a visible SKIP, not silently dropped.
    # TODO(host-gate refactor): run test_print_terminal.py under the VM suite gate.
    run_logged host qterminator-pytest "$EXIT_HOST" pytest "$WORKSPACE/qterminator" "$(host_pytest_cmd all 0 '' '--ignore=tests/test_print_terminal.py')" "excludes printer-coupled test_print_terminal.py (belongs in VM lane)"; step_rc=$?
    [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc
    record_skip host qterminator-print-tests pytest "tests/test_print_terminal.py excluded from host gate: QPrinter(HighResolution) blocks on the host default printer (Phaser-3020); move to VM lane (host-gate refactor)"

    # Extension repos: run tests + build. Coverage (REPORT-ONLY): when
    # @vitest/coverage-v8 is installed, run a SEPARATE non-gating vitest
    # --coverage step AFTER npm test to produce the coverage JSON artifact.
    # npm test always runs first for gating — it is never replaced.  A missing
    # coverage plugin is silently skipped and never gates the pipeline.
    _ext_cov_artifact_cmd() {
        # $1 = workspace dir, $2 = artifact dest dir (under $RDIR), $3 = repo label
        local _extdir=$1 _destdir=$2 _label=$3
        if [ -f "$_extdir/package.json" ] \
            && node -e "require('$_extdir/node_modules/@vitest/coverage-v8/package.json')" 2>/dev/null; then
            # Run coverage report-only; copy JSON artifact to run dir.
            echo "npx vitest run --coverage --coverage.reporter=text --coverage.reporter=json 2>/dev/null; _ec=\$?; [ -f coverage/coverage-final.json ] && mkdir -p \"$_destdir\" && cp coverage/coverage-final.json \"$_destdir/$_label-coverage.json\" || true; exit 0"
        else
            echo "true"
        fi
    }
    c="npm test && npm run build"
    run_logged host qdchrome-extension "$EXIT_HOST" npm "$WORKSPACE/qdchrome-extension" "$c" ""; step_rc=$?
    [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc
    c="$(_ext_cov_artifact_cmd "$WORKSPACE/qdchrome-extension" "$RDIR/host" "qdchrome-extension")"
    run_logged host qdchrome-extension-coverage "$EXIT_HOST" npm "$WORKSPACE/qdchrome-extension" "$c" "coverage (report-only)" || true
    coverage_floor_check qdchrome-extension "$RDIR/host/qdchrome-extension-coverage.json"; step_rc=$?
    [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc
    c="npm test && npm run build"
    run_logged host qdfirefox-extension "$EXIT_HOST" npm "$WORKSPACE/qdfirefox-extension" "$c" ""; step_rc=$?
    [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc
    c="$(_ext_cov_artifact_cmd "$WORKSPACE/qdfirefox-extension" "$RDIR/host" "qdfirefox-extension")"
    run_logged host qdfirefox-extension-coverage "$EXIT_HOST" npm "$WORKSPACE/qdfirefox-extension" "$c" "coverage (report-only)" || true
    coverage_floor_check qdfirefox-extension "$RDIR/host/qdfirefox-extension-coverage.json"; step_rc=$?
    [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc

    # NOTE: qdistro-site (the marketing website) is intentionally NOT built or
    # checked here — it ships through a separate website pipeline, not qci.
    RUN_STEP_TIMEOUT="$_saved_step_to"
    return "$rc"
}
