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

# qdwin/tests/gui/15-keybinding-events.md, full-20260914T194046Z-13620: all
# three REQUIRED asserts observed, only the scenario's own conditional 4.1
# skipped -- recorded ERROR. ERROR is graded as a hard failure either way, so a
# verdict-discipline slip like this costs a real red row.
@test "runtime prompt says skipped OPTIONAL steps do not make a scenario ERROR" {
    local p; p=$(_render_prompt)
    printf '%s' "$p" | grep -q 'REQUIRED assertions only'
    printf '%s' "$p" | grep -qi 'skipped, none failed. is PASS, never ERROR'
    # ERROR is a hard failure however the agent exits -- the cost of the slip.
    [ "$(gui_agent_verdict ERROR 0 | cut -f1)" = fail ]
}

# permissions-gui/15, /27, /39, /52 in full-20260918T143937Z-3516587: every
# functional oracle passed, then luna recorded ERROR because screenshot-fresh
# refused near-black frames. Those scenarios are qci:visual: none; the harness
# already leaves none PASS/FAIL alone, but the prompt told the agent the
# opposite ("captured nothing -> ERROR").
@test "runtime prompt does not make missing frames ERROR on qci:visual: none" {
    local p; p=$(_render_prompt)
    printf '%s' "$p" | grep -q 'qci:visual: none'
    printf '%s' "$p" | grep -qi 'not ERROR and not FAIL'
    printf '%s' "$p" | grep -qi 'screenshot-fresh refused'
    # The required-lane floor must still be stated, or this un-does the
    # visual-evidence contract. The phrase wraps in the prompt, so match
    # each half.
    printf '%s' "$p" | grep -qi 'no attested frame'
    printf '%s' "$p" | grep -qi 'recorded ERROR'
}

# permissions-gui/45 and /50, full-20260914T194046Z-13620: the agent killed a
# slow vm-exec and re-issued the driver; the first guest shell stayed alive and
# the two duplicated every request and row, so no verdict was attributable.
@test "runtime prompt forbids kill-and-reissue of a waiting vm-exec" {
    local p; p=$(_render_prompt)
    printf '%s' "$p" | grep -q 'NEVER kill a running'
    printf '%s' "$p" | grep -q 'vm-exec] Waiting'
    printf '%s' "$p" | grep -qi 'TRANSPORT IS'
}

# permissions-gui/50: a ROOT-owned shared /tmp log made the non-root launcher
# die with `Permission denied` and screenshot black. The warning must stay
# GENERIC: the launchers now write under $XDG_STATE_HOME/qdistro/, so pinning
# the obsolete /tmp/admin-app.log here would teach the agent to "repair" a path
# no shipped code opens (and to delete it as root).
# THE caller class that actually hangs GUI runs, and the one the static scanner
# cannot see: the driver scripts the agent writes at run time live under
# ci/runs/, which ci/bin/vmexec-fd2-scan.py skips by design. Counted over the
# archived runs in this checkout: 499 agent-written driver scripts, 121 of them
# opening with `exec > >(tee "$LOG") 2>&1`, 33 of those going on to call
# vm-exec. That shape puts vm-exec's fd 2 on tee's pipe, so every virsh/jq
# descendant inherits it and the reader waits for the LAST writer to close --
# measured at 4.00s against a vm-exec leaving a 4s descendant, where a file
# capture returned in 0.01s. Since CI cannot lint these files, the prompt is
# the only place the rule can live, and this test is what keeps it there.
@test "runtime prompt forbids a PIPE on vm-exec stderr in the agent's own driver" {
    local p; p=$(_render_prompt)
    printf '%s' "$p" | grep -q 'NEVER put a PIPE'
    printf '%s' "$p" | grep -q 'exec > >(tee'
    # It must give the replacement, not just the prohibition.
    printf '%s' "$p" | grep -q 'head -c'
    # Wrap-insensitive: the sentence is reflowed when the surrounding text
    # changes, and the RULE is what must be present, not its line breaks.
    printf '%s' "$p" | tr '\n' ' ' | grep -qi 'a file has no reader *to wait on'
    # The replacement it offers must name a variable the agent actually has.
    printf '%s' "$p" | grep -q 'QCI_GUI_ARTIFACT_DIR'
    if printf '%s' "$p" | grep -q '\$ART/'; then
        echo "prompt tells the agent to write to \$ART, which does not exist" >&2
        return 1
    fi
}

@test "runtime prompt warns that a fixed shared guest log may be root-owned" {
    local p; p=$(_render_prompt)
    printf '%s' "$p" | grep -q 'fixed shared guest path'
    printf '%s' "$p" | grep -qi 'ROOT-owned'
    printf '%s' "$p" | grep -q 'qci/.*\.log'
    # The obsolete remediation must NOT be pinned anywhere in the prompt.
    # Explicit refutation: a bare leading `!` does not fail a Bats test.
    if printf '%s' "$p" | grep -q '/tmp/admin-app.log'; then
        echo "prompt still pins the obsolete /tmp/admin-app.log remediation"; return 1
    fi
}

@test "runtime prompt requires one guest shell (the qdwin/21 EXIT-trap teardown)" {
    local p; p=$(_render_prompt)
    printf '%s' "$p" | grep -q 'ONE guest shell'
    printf '%s' "$p" | grep -qi 'EXIT.*trap'
}

@test "runtime prompt says vm-exec does not stream (permissions-gui/32 post-teardown frames)" {
    local p; p=$(_render_prompt)
    printf '%s' "$p" | grep -q 'vm-exec does NOT stream'
    printf '%s' "$p" | grep -q 'host-created marker file'
}

@test "runtime prompt: a tool re-capture to the same path is not tampering (permissions-gui/08)" {
    local p; p=$(_render_prompt)
    printf '%s' "$p" | grep -q 'the later one supersedes the earlier'
    printf '%s' "$p" | grep -q 'capture to NEW names'
}

@test "runtime prompt: a timed-out host-marker wait must not tear down (permissions-gui/25)" {
    local p; p=$(_render_prompt)
    printf '%s' "$p" | grep -q 'TIMEOUT path'
    printf '%s' "$p" | grep -q 'Gate cleanup on a host marker'
}

@test "runtime prompt: virsh/vm-gui/vm-exec are host commands, never in the guest driver (permissions-gui/22)" {
    local p; p=$(_render_prompt)
    printf '%s' "$p" | grep -q 'are HOST'
    printf '%s' "$p" | grep -q 'does not exist INSIDE the guest'
    printf '%s' "$p" | grep -q 'failed to get domain'
    # The domain it names must be the real VM, not an unexpanded variable.
    printf '%s' "$p" | grep -q 'libvirt domain `qci-vm-1`'
}

@test "runtime prompt: guest markers are invisible on the host (permissions-gui/13, 2026-09-25)" {
    local p; p=$(_render_prompt)
    printf '%s' "$p" | grep -q 'exists ONLY INSIDE THE GUEST'
    printf '%s' "$p" | grep -q 'silently, permanently false'
    # The example must carry the REAL slug, not an unexpanded variable.
    printf '%s' "$p" | grep -Fq "vm-exec \"\$VMNAME\" 'test -f /tmp/qci/"
    if grep -Fq '/tmp/qci/$slug/<marker>' <<<"$p"; then false; fi
}

@test "runtime prompt: the shell tool SIGKILLs a backgrounded vm-exec (permissions-gui/13, 2026-09-25)" {
    local p; p=$(_render_prompt)
    printf '%s' "$p" | grep -q 'YOUR SHELL TOOL SIGKILLS every process'
    printf '%s' "$p" | grep -q 'FOREGROUND of the command that owns it'
}

@test "runtime prompt: explains vm-exec exit 75 and says to retry once (blankss-pg13 r2)" {
    local p; p=$(_render_prompt)
    printf '%s' "$p" | grep -q 'vm-exec EXIT 75 means it REFUSED TO LAUNCH'
    printf '%s' "$p" | grep -q 'retry the SAME command once before diagnosing'
}
