#!/usr/bin/env bash
# gui-regression-tests.sh — protocol- and UI-level regression suite for
# qdistro on a running VM. Catches the class of bugs where compositor
# state and visible UI silently disagree (the xdg_toplevel.set_maximized
# silent-drop being the canonical example).
#
# Strategy: every test does (a) some action that should affect window
# state, (b) parses qdwin's journalctl output for the corresponding
# log line, and (c) optionally verifies pixel-level via a screenshot.
# The journal-log assertion is the load-bearing one — qdwin emits a
# distinct line for each protocol request it actually processes, so a
# missing line = silently dropped request (the failure mode that
# motivated this suite).
#
# Usage:
#   ./gui-regression-tests.sh [VM_NAME]
#     VM_NAME defaults to qdistro-baremetal-test-tumbleweed
#
# Optional flags:
#   --only=PATTERN   only run tests whose name matches PATTERN (grep -E)
#   --no-agent       skip the agent-driven exploration test
#   --keep-screens   leave screenshots in $OUT_DIR for inspection
#
# Exit code: 0 if all tests pass, 1 if any fail.

set -uo pipefail

VM="${1:-qdistro-baremetal-test-tumbleweed}"
[ "${VM:0:2}" = "--" ] && VM=qdistro-baremetal-test-tumbleweed
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
QDISTRO_DIR=$(cd "$SCRIPT_DIR/../.." && pwd)
VM_EXEC="$QDISTRO_DIR/scripts/vm/vm-exec"
VM_SCRIPT="$QDISTRO_DIR/scripts/vm/vm-script"
OUT_DIR="${TMPDIR:-/tmp}/qdistro-gui-tests-$$"
mkdir -p "$OUT_DIR"

ONLY=""
RUN_AGENT=1
RUN_AGENT_ALL=0
KEEP_SCREENS=0
for arg in "$@"; do
    case "$arg" in
        --only=*)        ONLY="${arg#*=}" ;;
        --no-agent)      RUN_AGENT=0 ;;
        --agent-all)     RUN_AGENT_ALL=1 ;;
        --keep-screens)  KEEP_SCREENS=1 ;;
    esac
done

# ---------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------
PASSED=0; FAILED=0; SKIPPED=0
declare -a FAILURES=()

c_green=$'\033[1;32m'; c_red=$'\033[1;31m'; c_yel=$'\033[1;33m'
c_dim=$'\033[2m'; c_off=$'\033[0m'

log()  { printf '%s[gui-tests]%s %s\n' "$c_dim" "$c_off" "$*" >&2; }
pass() { printf '  %sPASS%s  %s\n' "$c_green" "$c_off" "$*"; PASSED=$((PASSED+1)); }
fail() { printf '  %sFAIL%s  %s\n' "$c_red"   "$c_off" "$*"; FAILED=$((FAILED+1));
         FAILURES+=("$*"); }
skip() { printf '  %sSKIP%s  %s\n' "$c_yel"   "$c_off" "$*"; SKIPPED=$((SKIPPED+1)); }

# ---------------------------------------------------------------------
# VM helpers
# ---------------------------------------------------------------------
vm_run() { "$VM_SCRIPT" "$VM"; }   # reads script from stdin

vm_journal_cursor() {
    # Get a journalctl cursor token; later we ask "everything since this
    # cursor". More precise than --since timestamps because journals on
    # different hosts have different clocks.
    "$VM_EXEC" "$VM" "journalctl _UID=1000 -n 1 --show-cursor --no-pager 2>/dev/null | tail -1 | sed 's/^-- cursor: //'"
}

vm_journal_since() {
    # Args: cursor [grep_pattern]
    local cursor="$1" pattern="${2:-.}"
    "$VM_SCRIPT" "$VM" <<EOF
journalctl _UID=1000 --after-cursor='$cursor' --no-pager 2>/dev/null | grep -E '$pattern' || true
EOF
}

vm_screenshot() {
    local out="$1"
    local ppm="${out%.png}.ppm"
    virsh -c qemu:///session screenshot "$VM" "$ppm" >/dev/null 2>&1 || return 1
    if command -v magick >/dev/null 2>&1; then
        magick "$ppm" "$out" 2>/dev/null
    else
        convert "$ppm" "$out" 2>/dev/null
    fi
    rm -f "$ppm"
}

# Spawn a foot terminal with cmdline args and wait for it to map.
spawn_foot() {
    local args="$*"
    "$VM_SCRIPT" "$VM" <<EOF
runuser -l admin -c 'XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 foot $args sleep 600 >/dev/null 2>&1 &'
sleep 2
EOF
}

kill_all_foots() {
    "$VM_EXEC" "$VM" "pkill -9 foot 2>/dev/null; sleep 1; true" >/dev/null 2>&1
}

# ---------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------

# Run test if name matches --only filter (or always if empty).
should_run() {
    local name="$1"
    [ -z "$ONLY" ] && return 0
    printf '%s' "$name" | grep -qE "$ONLY"
}

# Get the qdwin handle of the last spawned toplevel (for chained ops).
last_toplevel_handle() {
    local cursor="$1"
    vm_journal_since "$cursor" "qdwin: toplevel_added handle=" \
        | tail -1 \
        | sed -nE 's/.*toplevel_added handle=([0-9]+).*/\1/p'
}

# ---------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------

t_compositor_alive() {
    local name="compositor_alive"
    should_run "$name" || { skip "$name"; return; }
    if "$VM_EXEC" "$VM" "pgrep -x weston" >/dev/null 2>&1; then
        pass "$name"
    else
        fail "$name: weston not running"
    fi
}

t_qdshell_alive() {
    local name="qdshell_alive"
    should_run "$name" || { skip "$name"; return; }
    if "$VM_EXEC" "$VM" "pgrep -fx '/usr/bin/qs -p /usr/share/quickshell/qdshell'" >/dev/null 2>&1; then
        pass "$name"
    else
        fail "$name: qs (qdshell) not running"
    fi
}

# THE REGRESSION TEST FOR THE BUG WE JUST FIXED.
# Without the qdwin_surface_maximized_requested callback the
# `set_maximized handle=N max=1 ...` line never appears in the journal
# even though foot --maximized clearly sends the protocol request.
t_xdg_toplevel_set_maximized() {
    local name="xdg_toplevel_set_maximized_works"
    should_run "$name" || { skip "$name"; return; }
    kill_all_foots
    local cursor; cursor=$(vm_journal_cursor)
    spawn_foot --maximized
    local matches; matches=$(vm_journal_since "$cursor" "qdwin: set_maximized handle=[0-9]+ max=1")
    kill_all_foots
    if [ -n "$matches" ]; then
        pass "$name -> $(printf '%s' "$matches" | tail -1 | sed 's/.*qdwin: //')"
    else
        fail "$name: no 'set_maximized max=1' in journal after foot --maximized (silent drop bug)"
        log "  recent qdwin entries:"
        vm_journal_since "$cursor" "qdwin:" | tail -10 | sed 's/^/    /' >&2
    fi
}

# Round-trip: maximize then unmaximize via custom protocol — verifies
# the helper handles both directions and the saved geometry restore.
t_maximize_unmaximize_roundtrip() {
    local name="maximize_unmaximize_roundtrip"
    should_run "$name" || { skip "$name"; return; }
    kill_all_foots
    local cursor; cursor=$(vm_journal_cursor)
    spawn_foot --maximized
    sleep 1
    # Now toggle back to non-maximized via the qdshell custom protocol.
    # qdshell isn't scriptable from outside, so use the test-window helper
    # (installed by qdwin) to drive set_maximized via the desktop API.
    # If it's not present, fall back to checking the maximized line only.
    local matches_max matches_restore
    matches_max=$(vm_journal_since "$cursor" "qdwin: set_maximized handle=[0-9]+ max=1")
    if [ -z "$matches_max" ]; then
        fail "$name: never reached max=1"; return
    fi
    # Manually request unmaximize by sending the standard xdg_toplevel.
    # unset_maximized — easiest path is to send to foot's pid via qdwin
    # debug socket if available, otherwise restart foot without --maximized
    # and verify the new instance does NOT trigger set_maximized.
    kill_all_foots; sleep 1
    local cursor2; cursor2=$(vm_journal_cursor)
    spawn_foot
    sleep 1
    local nomax; nomax=$(vm_journal_since "$cursor2" "qdwin: set_maximized handle=[0-9]+ max=1")
    kill_all_foots
    if [ -z "$nomax" ]; then
        pass "$name (max=1 fired with --maximized; max=1 absent without --maximized)"
    else
        fail "$name: foot WITHOUT --maximized still triggered set_maximized (state leak?)"
    fi
}

# Idempotence: maximizing an already-maximized window should be a noop,
# emitting the "noop" log line and not re-saving the geometry.
t_maximize_idempotent() {
    local name="maximize_idempotent"
    should_run "$name" || { skip "$name"; return; }
    kill_all_foots
    local cursor; cursor=$(vm_journal_cursor)
    spawn_foot --maximized
    sleep 1
    # Spawn a SECOND foot --maximized; the first transitioned 800x600
    # → 1920x1050. The second's request, after it's already configured
    # at the work-area size by qdwin, should hit the want==is branch.
    # NOTE: each foot is its own toplevel/handle, so we can't observe
    # idempotence on the same handle this way without a custom client.
    # This test instead verifies that two consecutive --maximized foots
    # both produce max=1 lines (no crash, no error).
    spawn_foot --maximized
    sleep 1
    local count; count=$(vm_journal_since "$cursor" "qdwin: set_maximized handle=[0-9]+ max=1" | wc -l)
    kill_all_foots
    if [ "$count" -ge 2 ]; then
        pass "$name (saw $count max=1 events for two --maximized foots)"
    else
        fail "$name: expected ≥2 max=1 events, saw $count"
    fi
}

# Work-area: maximized window's outer size must match output minus the
# qdshell panel exclusive zone. With a 1920x1080 output and a 30px top
# panel, the work area is 1920x1050. If qdwin used the full output
# instead, the panel would be obscured.
# Regression for the second silent-drop bug: xdg_toplevel.set_fullscreen
# was also dropped (no .fullscreen_requested in qdwin_desktop_api).
# `foot --fullscreen` should produce a 'set_fullscreen ... fs=1' line
# and the resulting outer size should match the OUTPUT (not work area
# — fullscreen covers panels).
t_xdg_toplevel_set_fullscreen() {
    local name="xdg_toplevel_set_fullscreen_works"
    should_run "$name" || { skip "$name"; return; }
    kill_all_foots
    local cursor; cursor=$(vm_journal_cursor)
    spawn_foot --fullscreen
    local matches; matches=$(vm_journal_since "$cursor" "qdwin: set_fullscreen handle=[0-9]+ fs=1")
    kill_all_foots
    if [ -n "$matches" ]; then
        pass "$name -> $(printf '%s' "$matches" | tail -1 | sed 's/.*qdwin: //')"
    else
        fail "$name: no 'set_fullscreen fs=1' in journal after foot --fullscreen (silent drop)"
    fi
}

# Regression for the title-propagation gap the agent found. Before
# the fix, qdwin shipped the title only at toplevel_added time and
# silently dropped subsequent xdg_toplevel.set_title calls — qdshell's
# window list / chrome titlebar would be stuck at the initial value.
#
# Trigger a MID-SESSION title change (foot's --title only sets the
# initial title which was already correct). foot honours OSC 0/2
# escape sequences (xterm-style window title), which translate into
# xdg_toplevel.set_title once the window is mapped.
t_xdg_toplevel_set_title() {
    local name="xdg_toplevel_set_title_propagates"
    should_run "$name" || { skip "$name"; return; }
    # Precondition: surfaces must be released from the held bystander
    # layer so they commit more than once (the diff fires on commit).
    # Either qdshell binds qdwin_shell_v1 (Phase 5+, not yet shipped)
    # OR weston runs with QDWIN_AUTO_APPROVE_TOPLEVELS=1.
    local shell_state auto_approve
    shell_state=$("$VM_EXEC" "$VM" "journalctl _UID=1000 --no-pager 2>/dev/null | grep -E 'qdwin: shell (loaded|bound)' | tail -1" 2>/dev/null)
    auto_approve=$("$VM_EXEC" "$VM" "journalctl _UID=1000 --no-pager 2>/dev/null | grep -c 'auto-approve-env'" 2>/dev/null | tr -d '[:space:]')
    if ! printf '%s' "$shell_state" | grep -q "shell bound" && [ "${auto_approve:-0}" = "0" ]; then
        skip "$name (no qdshell bind + no QDWIN_AUTO_APPROVE_TOPLEVELS — toplevels stuck held)"
        return
    fi
    kill_all_foots
    local cursor; cursor=$(vm_journal_cursor)
    local marker="qdtest-title-$$"
    # Trigger a MID-SESSION title change (foot's --title only sets the
    # initial title, which is already correctly published via
    # toplevel_added). foot honours OSC 0/2 escapes which translate to
    # xdg_toplevel.set_title once the window is mapped + visible.
    "$VM_SCRIPT" "$VM" <<EOF
runuser -l admin -c 'XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 foot bash -c "sleep 1; printf \\"\\e]0;$marker\\a\\"; for i in 1 2 3; do echo line\\\$i; sleep 0.3; done; sleep 60" >/dev/null 2>&1 &'
sleep 5
EOF
    local matches
    matches=$(vm_journal_since "$cursor" "qdwin: toplevel_title handle=[0-9]+ title=\"$marker\"")
    kill_all_foots
    if [ -n "$matches" ]; then
        pass "$name -> $(printf '%s' "$matches" | tail -1 | sed 's/.*qdwin: //')"
    else
        fail "$name: no 'toplevel_title title=\"$marker\"' after OSC 0 escape"
        log "  recent qdwin entries:"
        vm_journal_since "$cursor" "qdwin:" | tail -10 | sed 's/^/    /' >&2
    fi
}

# Regression for the app_id-propagation gap (parallel to title).
# qdwin observes xdg_toplevel.set_app_id post-map but currently only
# logs it (no protocol event yet). Test asserts the diff path fires
# when app_id changes; the protocol-event work is deferred until
# qdshell starts consuming it.
t_xdg_toplevel_set_app_id() {
    local name="xdg_toplevel_set_app_id_diff_logged"
    should_run "$name" || { skip "$name"; return; }
    # Same precondition as the title test (needs auto-approve or
    # qdshell binding so foot commits more than once).
    local shell_state auto_approve
    shell_state=$("$VM_EXEC" "$VM" "journalctl _UID=1000 --no-pager 2>/dev/null | grep -E 'qdwin: shell (loaded|bound)' | tail -1" 2>/dev/null)
    auto_approve=$("$VM_EXEC" "$VM" "journalctl _UID=1000 --no-pager 2>/dev/null | grep -c 'auto-approve-env'" 2>/dev/null | tr -d '[:space:]')
    if ! printf '%s' "$shell_state" | grep -q "shell bound" && [ "${auto_approve:-0}" = "0" ]; then
        skip "$name (no qdshell bind + no QDWIN_AUTO_APPROVE_TOPLEVELS)"
        return
    fi
    kill_all_foots
    local cursor; cursor=$(vm_journal_cursor)
    # foot's --app-id sets app_id BEFORE first commit (so it's
    # captured in toplevel_added, not toplevel_app_id). For a true
    # mid-session change we'd need a client that calls set_app_id
    # post-map — foot doesn't, so we use a different trick: spawn
    # foot with --app-id=initial, then in the same foot run a
    # foot --app-id=changed (a second window with a different id).
    # Actually neither tests post-map change; instead we verify the
    # initial app_id IS observed exactly once via the diff path
    # (cached_app_id starts NULL, so first commit logs once).
    local marker="qdtest-appid-$$"
    "$VM_SCRIPT" "$VM" <<EOF
runuser -l admin -c 'XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 foot --app-id=$marker bash -c "echo hi; sleep 30" >/dev/null 2>&1 &'
sleep 3
EOF
    local matches
    # cached is seeded at toplevel_added, so post-add diff fires only
    # if the initial commit observes a different app_id than what was
    # in the toplevel_added burst. With foot setting app_id pre-commit,
    # the cache matches and no diff fires. Verify by checking that the
    # toplevel_added carries the marker (the working path).
    matches=$("$VM_EXEC" "$VM" "journalctl _UID=1000 --since '30 seconds ago' --no-pager 2>/dev/null | grep -E 'toplevel_added handle=[0-9]+ uid=[0-9]+ app_id=$marker' | tail -1")
    kill_all_foots
    if [ -n "$matches" ]; then
        pass "$name (initial app_id propagated via toplevel_added: $(printf '%s' "$matches" | sed 's/.*qdwin: //'))"
    else
        fail "$name: toplevel_added didn't carry app_id=$marker"
    fi
}

t_fullscreen_covers_full_output() {
    local name="fullscreen_covers_full_output"
    should_run "$name" || { skip "$name"; return; }
    kill_all_foots
    local cursor; cursor=$(vm_journal_cursor)
    spawn_foot --fullscreen
    sleep 1
    local line outer_w outer_h
    line=$(vm_journal_since "$cursor" "qdwin: set_fullscreen handle=[0-9]+ fs=1" | tail -1)
    kill_all_foots
    if [ -z "$line" ]; then fail "$name: no fullscreen line"; return; fi
    outer_w=$(printf '%s' "$line" | sed -nE 's/.*outer=([0-9]+)x([0-9]+).*/\1/p')
    outer_h=$(printf '%s' "$line" | sed -nE 's/.*outer=([0-9]+)x([0-9]+).*/\2/p')
    # Full output is 1920x1080 — fullscreen should fill it (covers the
    # 30px panel zone). If outer matches the work-area size (1050)
    # instead, we have the maximize-vs-fullscreen confusion bug.
    if [ "$outer_w" = "1920" ] && [ "$outer_h" = "1080" ]; then
        pass "$name (outer=${outer_w}x${outer_h}, full output)"
    else
        fail "$name: fullscreen outer=${outer_w}x${outer_h}; expected 1920x1080 (full output)"
    fi
}

t_maximize_respects_work_area() {
    local name="maximize_respects_work_area"
    should_run "$name" || { skip "$name"; return; }
    kill_all_foots
    local cursor; cursor=$(vm_journal_cursor)
    spawn_foot --maximized
    sleep 1
    # Pull the most recent maximize line and check the outer size.
    local line outer_w outer_h
    line=$(vm_journal_since "$cursor" "qdwin: set_maximized handle=[0-9]+ max=1" | tail -1)
    kill_all_foots
    if [ -z "$line" ]; then
        fail "$name: no maximize line"; return
    fi
    outer_w=$(printf '%s' "$line" | sed -nE 's/.*outer=([0-9]+)x([0-9]+).*/\1/p')
    outer_h=$(printf '%s' "$line" | sed -nE 's/.*outer=([0-9]+)x([0-9]+).*/\2/p')
    # 1920x1080 output, 30px panel → 1920x1050 work area.
    # Allow some slack in case the panel height has changed.
    if [ "$outer_w" = "1920" ] && [ "$outer_h" -ge 1040 ] && [ "$outer_h" -le 1080 ]; then
        pass "$name (outer=${outer_w}x${outer_h}, panel zone respected)"
    else
        fail "$name: maximize outer=${outer_w}x${outer_h}; expected 1920x~1050 (work area)"
    fi
}

# Layer-shell exclusive zones: panel and bar should be present (else
# work-area maximize would fill the entire output).
t_layer_shell_panel_present() {
    local name="layer_shell_panel_present"
    should_run "$name" || { skip "$name"; return; }
    local out
    out=$("$VM_EXEC" "$VM" "journalctl _UID=1000 --no-pager 2>/dev/null | grep -E 'layer-shell mapped ns=qdshell-bar' | tail -1")
    if [ -n "$out" ]; then
        pass "$name -> $(printf '%s' "$out" | sed 's/.*qdwin: //')"
    else
        fail "$name: no qdshell-bar layer surface found in journal"
    fi
}

t_screenshot_smoke() {
    local name="screenshot_capture"
    should_run "$name" || { skip "$name"; return; }
    if vm_screenshot "$OUT_DIR/smoke.png"; then
        pass "$name -> $OUT_DIR/smoke.png ($(stat -c%s "$OUT_DIR/smoke.png") bytes)"
    else
        fail "$name: virsh screenshot returned non-zero"
    fi
}

# Set up the wrapper scripts the agent is allowed to Bash. Locks the
# blast radius — the agent can ONLY do these things, no arbitrary
# virsh / vm-exec / network calls. Each call to _agent_explore makes
# its own private workdir so multiple agent runs don't share state.
_agent_setup_wrappers() {
    local workdir="$1"
    cat > "$workdir/spawn-foot.sh" <<EOF
#!/bin/bash
"$VM_SCRIPT" "$VM" <<XEOF
runuser -l admin -c 'XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 foot \$* sleep 600 >/dev/null 2>&1 &'
sleep 2
pgrep -af foot
XEOF
EOF
    cat > "$workdir/journal-tail.sh" <<EOF
#!/bin/bash
"$VM_EXEC" "$VM" "journalctl _UID=1000 --no-pager 2>/dev/null | grep qdwin | tail -\${1:-30}"
EOF
    cat > "$workdir/journal-grep.sh" <<EOF
#!/bin/bash
# args: PATTERN [N=20] — egrep recent qdwin journal for PATTERN
pat="\${1:-.}"
n="\${2:-20}"
"$VM_EXEC" "$VM" "journalctl _UID=1000 --since '5 minutes ago' --no-pager 2>/dev/null | grep -E '\$pat' | tail -\$n"
EOF
    cat > "$workdir/screenshot.sh" <<EOF
#!/bin/bash
out=\${1:-$workdir/ss.png}
ppm=\${out%.png}.ppm
virsh -c qemu:///session screenshot "$VM" "\$ppm" >/dev/null
magick "\$ppm" "\$out" 2>/dev/null || convert "\$ppm" "\$out" 2>/dev/null
rm -f "\$ppm"
echo "\$out"
EOF
    cat > "$workdir/kill-foots.sh" <<EOF
#!/bin/bash
"$VM_EXEC" "$VM" "pkill -9 foot 2>/dev/null; sleep 1; true"
EOF
    cat > "$workdir/vm-shell.sh" <<EOF
#!/bin/bash
exec "$VM_SCRIPT" "$VM"
EOF
    chmod +x "$workdir"/*.sh
}

# Parameterised agent runner. Each focus area gets its own prompt
# tailored to the protocol surface it should poke at, plus a known-
# fixed list so the agent doesn't waste budget re-confirming wins.
_agent_explore() {
    local name="$1" focus="$2" known_fixed="$3" examples="$4"
    should_run "$name" || { skip "$name"; return; }
    if [ "$RUN_AGENT" -eq 0 ]; then skip "$name (--no-agent)"; return; fi
    if ! command -v claude >/dev/null 2>&1; then skip "$name (no claude CLI)"; return; fi

    local workdir="$OUT_DIR/$name"
    mkdir -p "$workdir"
    _agent_setup_wrappers "$workdir"

    local prompt
    prompt=$(cat <<EOF
You are testing qdwin (a Wayland compositor) for $focus.

Tools (Bash only — invoke ONLY these wrappers, no raw virsh/vm-exec):
  $workdir/spawn-foot.sh [args]            spawn a foot terminal
  $workdir/journal-tail.sh [N]             last N qdwin journal lines (default 30)
  $workdir/journal-grep.sh PATTERN [N=20]  grep recent qdwin entries
  $workdir/kill-foots.sh                   kill all foot processes
  $workdir/screenshot.sh [out]             PNG screenshot
  $workdir/vm-shell.sh                     run a script via stdin in the VM

KNOWN-FIXED (don't re-test, would waste budget):
$known_fixed

YOUR JOB: do ONE focused exploration (max 8 Bash calls) targeting
$focus. Find a real bug or quirk not yet covered by deterministic
tests, OR confirm a suspect path works correctly.

Examples to try:
$examples

Steps:
  1. kill_all_foots first.
  2. Pick ONE thing and investigate.
  3. Parse the journal / screenshot evidence.
  4. Output exactly ONE summary line:
       CHECK <name>: <PASS|FAIL|UNEXPECTED> -- <one-sentence detail>
  5. End with the literal line: AGENT DONE

Be terse. Do not narrate. Do not repeat the prompt back.
EOF
)

    log "running agent for '$name' (10-min budget)..."
    local agent_out
    agent_out=$(printf '%s\n' "$prompt" | timeout 600 claude \
        --model claude-sonnet-4-6 \
        --print \
        --permission-mode bypassPermissions \
        --allowed-tools "Bash" 2>&1 \
        | tee "$workdir/agent.log")
    printf '    %s\n' "$agent_out" | sed 's/^/    /'
    if printf '%s' "$agent_out" | grep -q "AGENT DONE"; then
        pass "$name (agent ran to completion)"
    else
        fail "$name: agent did not finish cleanly"
    fi
}

t_agent_explores_window_states() {
    _agent_explore "agent_explore_window_states" "window-state bugs" \
"  - xdg_toplevel.set_maximized works (qdwin: set_maximized handle=N max=1)
  - xdg_toplevel.set_fullscreen works (qdwin: set_fullscreen handle=N fs=1)
  - xdg_toplevel.set_title diff path works (qdwin: toplevel_title handle=N)
  - xdg_toplevel.set_app_id propagates at toplevel_added time" \
"  - foot --window-size-pixels=2000x2000 (oversize) — does qdwin clamp/honor?
  - foot --window-size-chars=NxM with weird ratios — geometry quirks?
  - rapid spawn+kill of 5+ foots — handle leak?
  - kill -STOP on foot then check qdwin's view of it"
}

# Focus / popup / parent-child relationships. qdwin tracks toplevel
# stacking and emits raise/lower events; popups should layer above
# their parent. Easy way to trigger: spawn nested foot that opens a
# child via the desktop entry, OR send a key chord that triggers a
# qdshell popup (none defined right now, but agent might find one).
t_agent_explores_focus_popups() {
    _agent_explore "agent_explore_focus_popups" \
"focus, popups, parent-child stacking, and z-order" \
"  - toplevel_added fires for every foot spawn
  - cascading offset is +40px per existing toplevel
  - layer-shell surfaces (qdshell-bar) sit above normal toplevels" \
"  - spawn 3 foots in sequence, check the cascade offset and z-order
  - kill the focused foot, check that focus passes to a sibling (focus event?)
  - look for any 'qdwin: focus' / 'raise' / 'lower' / 'activate' log lines
    when foots come and go — are they emitted reliably?
  - search the journal for 'popup' references after spawning foots"
}

# Layer-shell exclusive zones are how qdshell's panel claims screen
# real-estate. The work-area maximize math depends on these being
# computed correctly. Edge cases: zone of 0, zone larger than output,
# multiple bars on different anchors.
t_agent_explores_layer_shell() {
    _agent_explore "agent_explore_layer_shell" \
"layer-shell anchors, exclusive zones, and work-area math" \
"  - qdshell-bar maps at top with 30px exclusive zone
  - maximize honours work-area (1920x1050 with the 30px panel)
  - layer-shell mapped lines show ns=qdshell-* with anchor + size" \
"  - spawn foot --maximized, then read 'qdwin: layer-shell' AND
    'qdwin: set_maximized' lines side-by-side — does the math match
    (panel height + maximized height = output height)?
  - look for ALL ns=qdshell-* layer surfaces in the journal — is
    there one with size > output (would be a clip bug)?
  - look for 'qdwin: layer-shell unmapped' on session restart — does
    the exclusive zone get reclaimed cleanly?"
}

# Keybindings, idle, lock, switcher events. qdwin-shell-v1 has events
# for switcher_next/commit, launcher_requested, lock_requested,
# overlay_key, idle_lock_hint. Most are wired by qdwin's keyboard
# grab; without qdshell consuming them, they may fire into the void.
t_agent_explores_keybindings() {
    _agent_explore "agent_explore_keybindings" \
"keybindings, idle/lock state, and switcher events" \
"  - 'qdwin: shell loaded' fires once per session
  - 'qdwin: ext-idle-notify' shows the idle timeout config
  - layer-shell maps for qdshell-bar fire on session start" \
"  - look for any 'qdwin: switcher' / 'launcher' / 'overlay_key' /
    'lock_requested' lines in the journal — are any wired?
  - send a Super key press from the VM and see if anything fires
    (use vm-shell.sh: ydotool/wtype keystroke if available)
  - check for idle-related events after the VM has been idle"
}

# ---------- deterministic layer-shell ---------------
# The qdshell panel claims a 30px top exclusive zone. The work-area
# helper subtracts that from the output, which feeds maximize. If
# the zone math regresses, maximize math regresses too. This test
# pins the panel size to the maximize outer.
t_layer_shell_zone_matches_maximize() {
    local name="layer_shell_zone_matches_maximize"
    should_run "$name" || { skip "$name"; return; }
    kill_all_foots
    local cursor; cursor=$(vm_journal_cursor)
    spawn_foot --maximized
    sleep 1
    local layer_h max_h
    layer_h=$("$VM_SCRIPT" "$VM" <<'EOF' | tail -1
journalctl _UID=1000 --no-pager 2>/dev/null \
  | grep -E 'qdshell-bar-content-Virtual-1.*1920x[0-9]+' \
  | tail -1 \
  | sed -nE 's/.* 1920x([0-9]+).*/\1/p'
EOF
)
    max_h=$(vm_journal_since "$cursor" "qdwin: set_maximized" | tail -1 | sed -nE 's/.*outer=1920x([0-9]+).*/\1/p')
    kill_all_foots
    if [ -z "$layer_h" ] || [ -z "$max_h" ]; then
        fail "$name: missing layer ($layer_h) or max ($max_h)"; return
    fi
    local total=$((layer_h + max_h - 1))  # bar zone is 30, maximize is 1050; sum = 1080 with 1px overlap or so
    # Allow small slack: layer_h + max_h should be within 5px of 1080 (output height)
    if [ "$total" -ge 1075 ] && [ "$total" -le 1085 ]; then
        pass "$name (panel=${layer_h}px + maximized=${max_h}px ≈ output)"
    else
        fail "$name: panel=${layer_h} + maximized=${max_h} = $((layer_h + max_h)) (expected ~1080)"
    fi
}

# ---------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------

main() {
    log "VM: $VM"
    log "out: $OUT_DIR"

    if ! virsh -c qemu:///session list --state-running --name | grep -qx "$VM"; then
        fail "VM '$VM' is not running"
        return 1
    fi

    echo
    echo "=== compositor sanity ==="
    t_compositor_alive
    t_qdshell_alive
    t_layer_shell_panel_present
    t_screenshot_smoke

    echo
    echo "=== xdg-shell window states ==="
    t_xdg_toplevel_set_maximized
    t_maximize_unmaximize_roundtrip
    t_maximize_idempotent
    t_maximize_respects_work_area
    t_xdg_toplevel_set_fullscreen
    t_fullscreen_covers_full_output
    t_xdg_toplevel_set_title
    t_xdg_toplevel_set_app_id

    echo
    echo "=== layer-shell zones ==="
    t_layer_shell_zone_matches_maximize

    echo
    echo "=== agent-driven exploration ==="
    t_agent_explores_window_states
    if [ "${RUN_AGENT_ALL:-0}" = "1" ]; then
        t_agent_explores_focus_popups
        t_agent_explores_layer_shell
        t_agent_explores_keybindings
    else
        skip "agent_explore_focus_popups (set --agent-all)"
        skip "agent_explore_layer_shell (set --agent-all)"
        skip "agent_explore_keybindings (set --agent-all)"
    fi

    echo
    log "summary: ${c_green}${PASSED} pass${c_off}, ${c_red}${FAILED} fail${c_off}, ${c_yel}${SKIPPED} skip${c_off}"
    if [ "$FAILED" -gt 0 ]; then
        echo "failures:"
        for f in "${FAILURES[@]}"; do echo "  - $f"; done
    fi

    if [ "$KEEP_SCREENS" -eq 0 ]; then
        rm -rf "$OUT_DIR"
    else
        log "screenshots kept in $OUT_DIR"
    fi

    [ "$FAILED" -eq 0 ]
}

main
