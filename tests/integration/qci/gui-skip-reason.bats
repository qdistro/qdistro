#!/usr/bin/env bats
#
# Host-only tests for GUI skip-reason propagation
# (ci/lib/gates/gui.sh::gui_skip_reason). No VM: the function reads only an
# artifact directory.
#
# Why this exists: gui_agent_verdict is pure (status + rc) and returns the fixed
# note "agent scenario skipped", which was written verbatim into results.tsv. So
# every skipped GUI scenario looked identical, and report.py's
# dependency-missing detector could not tell a GOLDEN-IMAGE GAP ("foot is not
# installed in the guest") from a legitimately not-applicable scenario. The
# 2026-09-09 full run's qdlocker/tests/gui/01-lock-cycle.md is the first kind and
# took 747s to reach that conclusion invisibly.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    ADIR="$BATS_TEST_TMPDIR/artifacts"
    mkdir -p "$ADIR"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/core.sh"
    source "$REPO_ROOT/ci/lib/gates/gui.sh"
}

@test "status.txt carrying an inline reason wins" {
    printf 'SKIP foot not installed in guest\n' > "$ADIR/status.txt"
    [ "$(gui_skip_reason "$ADIR")" = "foot not installed in guest" ]
}

@test "a bare SKIP falls through to the report.md Result: line" {
    printf 'SKIP\n' > "$ADIR/status.txt"
    printf '# 01-lock-cycle.md — SKIP\n\nResult: SKIP because `foot` is not installed in the guest.\n' \
        > "$ADIR/report.md"
    run gui_skip_reason "$ADIR"
    [[ "$output" == *"foot"* ]]
    [[ "$output" == *"not installed"* ]]
    # Backticks are flattened — the value lands in a TSV note column.
    [[ "$output" != *'`'* ]]
}

@test "with no Result: line, the first prose line is used (not the heading)" {
    printf '# 07-xwayland.md — SKIP\n\nXWayland lane is opt-in (QDWIN_XWAYLAND=1).\n' \
        > "$ADIR/report.md"
    run gui_skip_reason "$ADIR"
    [[ "$output" == "XWayland lane is opt-in (QDWIN_XWAYLAND=1)." ]]
}

@test "markdown links are flattened to their text" {
    printf 'Result: skipped; see [setup.log](setup.log) for detail.\n' > "$ADIR/report.md"
    run gui_skip_reason "$ADIR"
    [[ "$output" == *"see setup.log for detail."* ]]
    [[ "$output" != *"]("* ]]
}

@test "a bare SKIP with no report yields NOTHING (caller keeps its generic note)" {
    printf 'SKIP\n' > "$ADIR/status.txt"
    # `run` + an explicit status assertion: a bare [ -z "$(...)" ] would also
    # pass if the helper did not exist and only printed to stderr.
    run gui_skip_reason "$ADIR"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "an empty artifact dir yields nothing and does not error" {
    run gui_skip_reason "$ADIR"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "a symlinked status.txt/report.md is not read (harvest integrity)" {
    printf 'SKIP attacker supplied\n' > "$BATS_TEST_TMPDIR/elsewhere.txt"
    ln -s "$BATS_TEST_TMPDIR/elsewhere.txt" "$ADIR/status.txt"
    ln -s "$BATS_TEST_TMPDIR/elsewhere.txt" "$ADIR/report.md"
    run gui_skip_reason "$ADIR"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "the reason is length-capped" {
    { printf 'Result: '; printf 'x%.0s' {1..900}; printf '\n'; } > "$ADIR/report.md"
    run gui_skip_reason "$ADIR"
    [ "$status" -eq 0 ]
    [ "${#output}" -le 300 ]
}

@test "EVERY control character is stripped, not just tab and newline" {
    # report.py reads the TSV with Python splitlines(), which breaks on \x0b
    # \x0c \x1c \x1d \x1e \x85 U+2028 U+2029 as well as \n. Stripping only
    # tab/LF left a reason able to split one result into two malformed rows —
    # and an ESC able to inject a terminal escape sequence into the report.
    printf 'SKIP a\x0bb\x0cc\x1cd\x1de\x1ef\x1b[31mg\th\n' > "$ADIR/status.txt"
    run gui_skip_reason "$ADIR"
    [ "$status" -eq 0 ]
    run python3 -c "
import sys
v = sys.argv[1]
assert len(v.splitlines()) <= 1, 'splits into %d lines' % len(v.splitlines())
assert not any(ord(c) < 32 or ord(c) == 127 for c in v), 'control char survived'
" "$output"
    [ "$status" -eq 0 ]
}

@test "Unicode line separators are neutralised too" {
    printf 'SKIP a\xe2\x80\xa8b\xe2\x80\xa9c\xc2\x85d\n' > "$ADIR/status.txt"
    run gui_skip_reason "$ADIR"
    [ "$status" -eq 0 ]
    run python3 -c "
import sys
assert len(sys.argv[1].splitlines()) <= 1
" "$output"
    [ "$status" -eq 0 ]
}

@test "a hostile reason round-trips through record_result as exactly ONE report row" {
    # End-to-end: the sanitized note must survive the real TSV writer and the
    # real report reader as a single, complete 9-column row.
    printf 'SKIP broke\x1ehere\x0bnow\n' > "$ADIR/status.txt"
    local reason; reason="$(gui_skip_reason "$ADIR")"

    RDIR="$BATS_TEST_TMPDIR/run"; mkdir -p "$RDIR"
    printf 'gate	subject	status	exit_code	exit_class	kind	log	notes	category
' \
        > "$RDIR/results.tsv"
    category_for() { printf 'gui'; }
    rel_path() { printf '%s' "$1"; }
    record_dest() { printf '%s/results.tsv' "$RDIR"; }
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/run.sh"
    record_result gui "scenario.md" skip 0 pass gui "" "agent scenario skipped: $reason"

    run python3 -c "
import importlib.util, sys
spec = importlib.util.spec_from_file_location('r', '$REPO_ROOT/ci/lib/report.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
rows = m.read_tsv(__import__('pathlib').Path('$RDIR/results.tsv'))
assert len(rows) == 1, 'expected 1 row, got %d' % len(rows)
assert rows[0]['status'] == 'skip', rows[0]
assert rows[0]['category'] == 'gui', 'row truncated: %r' % rows[0]
"
    [ "$status" -eq 0 ]
}

@test "the resulting note is recognised by report.py's dependency-missing detector" {
    # The end-to-end point of the change: a golden-image gap must be COUNTED as
    # one, instead of hiding in a green-looking mostly-skipped run.
    printf 'SKIP\n' > "$ADIR/status.txt"
    printf 'Result: SKIP during setup because `foot` is not installed in the guest.\n' \
        > "$ADIR/report.md"
    note="agent scenario skipped: $(gui_skip_reason "$ADIR")"
    run python3 -c "
import importlib.util, sys
spec = importlib.util.spec_from_file_location('r', '$REPO_ROOT/ci/lib/report.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
sys.exit(0 if m._DEP_MISSING_RE.search(sys.argv[1].lower()) else 1)
" "$note"
    [ "$status" -eq 0 ]
}

@test "the generic note ALONE is not a dependency gap (no false positives)" {
    run python3 -c "
import importlib.util, sys
spec = importlib.util.spec_from_file_location('r', '$REPO_ROOT/ci/lib/report.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
sys.exit(0 if m._DEP_MISSING_RE.search('agent scenario skipped') else 1)
"
    [ "$status" -ne 0 ]
}

# --- the prompt/harness contract itself -------------------------------------
#
# These pin the RUNTIME prompt (write_agent_prompt) against the code that reads
# what it asks the agent to write. The documented template
# ci/prompts/gui-scenario-agent.md is not sourced at runtime, so the two drifted:
# the prompt said "Exit nonzero on ... missing precondition" and "status.txt
# containing exactly one word" while the merged harness accepts SKIP only with
# rc=0 and lifts the reason from the rest of that line. A literal-minded agent
# obeying that prompt produced a false red on
# qdwin/tests/gui/21-wm-policy-bystander.md in full-20260909T224527Z-2330163.

_render_prompt() {
    RDIR="$BATS_TEST_TMPDIR/run"
    QDISTRO_REPO="$BATS_TEST_TMPDIR/qdistro"
    mkdir -p "$RDIR" "$QDISTRO_REPO"
    write_agent_prompt \
        qci-vm-1 "$QDISTRO_REPO/tests/gui/21.md" "$BATS_TEST_TMPDIR/prompt.txt" \
        "$ADIR" "$BATS_TEST_TMPDIR/scratch" slug
    cat "$BATS_TEST_TMPDIR/prompt.txt"
}

@test "runtime prompt states the SKIP-with-rc=0 rule the harness enforces" {
    local p; p=$(_render_prompt)
    # gui_agent_verdict accepts SKIP ONLY with rc=0; a nonzero-rc SKIP is a fail.
    [ "$(gui_agent_verdict SKIP 0 | cut -f1)" = skip ]
    [ "$(gui_agent_verdict SKIP 1 | cut -f1)" = fail ]
    printf '%s' "$p" | grep -q 'SKIP.*exit 0'
    printf '%s' "$p" | grep -qi 'NONZERO exit is a hard failure'
}

@test "runtime prompt no longer tells the agent to exit nonzero on a precondition" {
    local p; p=$(_render_prompt)
    ! printf '%s' "$p" | grep -q 'Return nonzero on FAIL or ERROR'
    ! printf '%s' "$p" | grep -qi 'exit nonzero on .*missing precondition'
}

@test "runtime prompt asks for a same-line SKIP reason that gui_skip_reason reads" {
    local p; p=$(_render_prompt)
    printf '%s' "$p" | grep -q 'SKIP <reason>'
    # The syntax the prompt asks for must survive BOTH readers: the verdict
    # parser must see SKIP, and the reason parser must see the rest of the line.
    printf 'SKIP foot is not installed in this golden image (command -v foot -> not found)\n' \
        > "$ADIR/status.txt"
    [ "$(agent_artifact_status "$ADIR" /dev/null)" = SKIP ]
    [ "$(gui_status_file_verdict "$ADIR/status.txt")" = SKIP ]
    [ "$(gui_skip_reason "$ADIR")" = "foot is not installed in this golden image (command -v foot -> not found)" ]
}

@test "runtime prompt keeps SKIP narrow and routes agent-side failures to ERROR" {
    local p; p=$(_render_prompt)
    printf '%s' "$p" | grep -qi 'SKIP is deliberately NARROW'
    printf '%s' "$p" | grep -qi 'choose ERROR'
    # The qdwin/21 shape -- a process that started and then died -- must be named
    # as NOT a skip, or the S2 fix turns that real defect green.
    printf '%s' "$p" | grep -qi 'started and then died'
    # ERROR stays a hard failure however the agent exits.
    [ "$(gui_agent_verdict ERROR 0 | cut -f1)" = fail ]
    [ "$(gui_agent_verdict ERROR 1 | cut -f1)" = fail ]
}

@test "runtime prompt requires one guest shell (the qdwin/21 EXIT-trap teardown)" {
    local p; p=$(_render_prompt)
    printf '%s' "$p" | grep -q 'ONE guest shell'
    printf '%s' "$p" | grep -qi 'EXIT.*trap'
}
