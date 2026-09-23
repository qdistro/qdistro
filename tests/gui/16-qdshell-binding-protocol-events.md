# 16 — qdshell binds qdwin_shell_v1 and observes protocol events

<!-- qci:visual: none -->

**Acceptance criterion:** qdshell (via the `Qdistro.Qdwin` QML plugin
that loads `libqdistro-qdwin.so` at startup) binds `qdwin_shell_v1`
at version >= 14, calls `bind_as_shell`, and observes the full
protocol-event stream: `hello`, every `toplevel_added` /
`toplevel_removed`, and every `seat_focus_changed`. The QML side's
`Qdwin` singleton's `windows` ListModel matches qdwin's view of
live toplevels at all times — no extra rows, no missing rows, no
stale titles.

This is the deepest end-to-end assertion that workstream A (the
qdshell ↔ qdwin_shell_v1 binding, landed 2026-05-14) is functional
in both directions: events go server → client AND the client
materially uses them to maintain state.

## Prerequisites

- qdshell built with `meson compile -C build` so
  `qdshell/build/qml-plugin/libqdistro-qdwin.so` exists.
- `install-qdwin-session-for-vm.sh` has installed the plugin to
  `/usr/share/qdistro/qml/Qdistro/Qdwin/` and dropped in the
  `qdshell.service` `QML_IMPORT_PATH` Environment. Without
  these, the QML import fails silently and qdshell runs with the
  no-binding fallback stubs.
- `qs` / `noctalia-qs` 0.0.12+ installed on the VM.
- `qdistro-test-window` on the VM PATH (the same reliable test client
  the qdwin SMOKES use; it deterministically produces a qdwin toplevel
  with `app_id=qdistro-test-window`). This scenario uses it instead of
  `foot`, which is not installed on the VM and so cannot exercise the
  protocol-event paths.

Fail loudly if any prereq is missing; do not skip.

## Setup

```bash
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"
qdwin_session_healthy || { echo "FAIL: session not up"; exit 1; }

"$QDWIN_VM_EXEC" "$VMNAME" 'test -f /usr/share/qdistro/qml/Qdistro/Qdwin/libqdistro-qdwin.so' \
    || { echo "FAIL: QML plugin not installed on VM"; exit 1; }

# PRECONDITION (infra): the test client must be installed. Absence is an
# ERROR (the scenario cannot be exercised), NOT a product FAIL.
"$QDWIN_VM_EXEC" "$VMNAME" 'command -v qdistro-test-window >/dev/null' \
    || { echo "ERROR: qdistro-test-window not installed on VM (cannot exercise protocol events)"; exit 1; }

"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -f "[q]distro-test-window" 2>/dev/null; sleep 1' >/dev/null

# PRECONDITION (infra): bar text needs real fontconfig faces. Goldens baked
# with QDWIN_APP_DEPS=0 used to ship without dejavu/liberation, which left
# FontService at "Loaded 1 fonts, 1 monospace" and blanked every bar label
# (Clock, ActiveWindow title, workspace numbers) even when the binding was
# fine. Install the baseline faces if the guest has essentially none, then
# leave qdshell to pick them up on the Step 1 restart.
"$QDWIN_VM_EXEC" "$VMNAME" '
  n=$(fc-list : family 2>/dev/null | sed "/^$/d" | wc -l)
  echo "font-families-before=$n"
  if [ "${n:-0}" -lt 2 ]; then
    zypper -n install --no-recommends dejavu-fonts liberation-fonts >/dev/null 2>&1 || true
    fc-cache -f >/dev/null 2>&1 || true
  fi
  n2=$(fc-list : family 2>/dev/null | sed "/^$/d" | wc -l)
  echo "font-families-after=$n2"
  [ "${n2:-0}" -ge 1 ] || {
    echo "ERROR: no fontconfig families available (bar text cannot render; cannot exercise ActiveWindow title)"
    exit 1
  }
'
```

## Steps

### Step 1 — bind handshake visible in journal

Restart qdshell so the bind happens after our cursor. **The cursor MUST be
taken BEFORE the restart** — the bind lands within ~1s of the restart, so a
cursor read after the restart (or after any sleep following it) sits past
both lines and the asserts read an empty window. That is exactly how
full-20260922T193137Z-881799 recorded a false 1.1/1.2 FAIL: the runner took
the cursor after `restart` + `sleep 8`, while the compositor journal shows
`bind accepted for uid=1000` and the QML `bound v34` line both logged ~1s
after the restart. Run this block as written; do not reorder it.

```bash
CURSOR=$("$QDWIN_VM_EXEC" "$VMNAME" "journalctl _UID=1000 -n 1 \
  --show-cursor --no-pager 2>/dev/null | tail -1 | sed 's/^-- cursor: //'")
[ -n "$CURSOR" ] || { echo "ERROR: could not read a journal cursor before the restart"; exit 1; }
"$QDWIN_VM_EXEC" "$VMNAME" \
    "runuser -l admin -c 'XDG_RUNTIME_DIR=/run/user/1000 \
     systemctl --user restart qdshell.service'"

# Poll (bounded, ~20s) for BOTH lines after the pre-restart cursor. The greps
# are scoped by `_UID=1000`, which excludes qemu-ga's own `guest-exec called`
# echo of this command (qemu-ga runs as root), so the pattern text cannot
# match itself. Keep journalctl's default (short) output: Quickshell writes
# ANSI colour codes INTO the message (`Qdwin\e[0m qdwin_shell_v1 bound v34`),
# which short output strips and `-o cat` does not — so `-o cat` breaks the
# 1.2 pattern.
BIND_LINE= BOUND_LINE=
for _ in $(seq 1 40); do
    J=$("$QDWIN_VM_EXEC" "$VMNAME" \
        "journalctl _UID=1000 --after-cursor='$CURSOR' --no-pager 2>/dev/null | \
         grep -E 'qdwin: bind accepted for uid=1000|Qdwin +qdwin_shell_v1 bound v[0-9]+'")
    BIND_LINE=$(printf '%s\n' "$J" | grep -m1 'qdwin: bind accepted for uid=1000')
    BOUND_LINE=$(printf '%s\n' "$J" | grep -m1 -E 'Qdwin +qdwin_shell_v1 bound v[0-9]+')
    [ -n "$BIND_LINE" ] && [ -n "$BOUND_LINE" ] && break
    sleep 0.5
done
BOUND_V=$(printf '%s' "$BOUND_LINE" | sed -nE 's/.*qdwin_shell_v1 bound v([0-9]+).*/\1/p')
echo "1.1: ${BIND_LINE:-<absent>}"
echo "1.2: ${BOUND_LINE:-<absent>} (version=${BOUND_V:-?})"
```

**Assert (1.1):** `$BIND_LINE` is non-empty — `qdwin: bind accepted for
uid=1000` appears in the journal after `$CURSOR`.

**Assert (1.2):** `$BOUND_LINE` is non-empty and `$BOUND_V` is an integer
**>= 14** — the QML `INFO qml: ... Qdwin qdwin_shell_v1 bound v<N>` line.
The version is whatever the plugin negotiated (34 on the 2026-09 goldens);
the requirement is the floor, NOT a literal `v14`. (Both lines must appear —
qdwin's confirms the server saw the request, the QML one confirms the
plugin loaded and dispatched the `hello` event up to QML.)

### Step 2 — toplevel_added reaches QML

```bash
CURSOR=$("$QDWIN_VM_EXEC" "$VMNAME" "journalctl _UID=1000 -n 1 \
  --show-cursor --no-pager 2>/dev/null | tail -1 | sed 's/^-- cursor: //'")
# setsid -f detaches the client into its own session so it survives the
# vm_exec shell returning (a bare `&` gets SIGHUP'd and the toplevel
# never maps). Same launch pattern the qdwin smokes use.
"$QDWIN_VM_EXEC" "$VMNAME" \
    "setsid -f runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
     WAYLAND_DISPLAY=wayland-1 qdistro-test-window --title 'qd16-step2' \
     --width 300 --height 180 --color 0xff304050 >/tmp/qd16-step2.log 2>&1"
sleep 2

HANDLE=$("$QDWIN_VM_EXEC" "$VMNAME" \
  "journalctl _UID=1000 --after-cursor='$CURSOR' --no-pager | \
   grep -E 'qdwin: toplevel_added handle=[0-9]+ uid=1000 pid=[0-9]+ app_id=qdistro-test-window' | tail -1 | \
   sed -nE 's/.*handle=([0-9]+).*/\1/p'")
# Resolve the window's real PID from the SAME toplevel_added line so Step 4
# can close it by PID rather than by brittle command-line matching. The grep
# stays scoped to this spawn's `$CURSOR` and this run's `$HANDLE`, so it can
# never pick up a stale line from an earlier compositor session.
PID=$("$QDWIN_VM_EXEC" "$VMNAME" \
  "journalctl _UID=1000 --after-cursor='$CURSOR' --no-pager | \
   grep -E 'qdwin: toplevel_added handle=$HANDLE uid=1000 pid=[0-9]+ app_id=qdistro-test-window' | tail -1 | \
   sed -nE 's/.*pid=([0-9]+).*/\1/p'")
echo "subject handle=$HANDLE pid=$PID"
```

**Assert (2.1):** $HANDLE is a number (qdwin's `toplevel_added
handle=<N> uid=1000 pid=<N> app_id=qdistro-test-window` log line
fires).

**Assert (2.2):** the `qdwin: seat_focus_changed seat=default
handle=$HANDLE` line is present after `$CURSOR`. This line is
gated behind "a v14+ shell is bound" — it is the load-bearing
proof that the binding is exercising the protocol-emit branch
(prior to workstream A, this line was absent on a toplevel spawn,
even though the underlying `qdwin: focus handle=` ground-truth
line still fired).

### Step 3 — focused window visible in shell UI

The qdshell bar's ActiveWindow widget reads
`Qdwin.getFocusedWindowTitle()`. With the binding in place,
spawning the test window should make the bar reflect its title.

```bash
sleep 1
qdwin_screenshot /tmp/16-step3-window-focused.png
```

**Assert (3.1) — SOFT / NON-BLOCKING:** the screenshot's top-bar
region (y in [0, 31]) contains the substring "qd16-step2" (the
spawned window's title) — visible because the `ActiveWindow` widget
pulls from `Qdwin.windows`'s focused row. Use `tesseract` OCR or a
`convert -crop` + visual read of the rendered text.

**Runner rule for 3.1:** if 3.1 is the only failing assert, the
scenario overall result is still **PASS**. Record 3.1 as
`SOFT-FAIL` (diagnostic) in the report and write `status.txt` =
`PASS`. Do **not** fail the scenario on OCR flake, missing host
tesseract, or blank antialiased glyphs alone. A 3.1-only miss is
informative (fonts missing, ListModel title empty, or OCR noise) and
belongs in the report body — not as a hard FAIL. Hard FAIL only when
a required assert (1.x / 2.x / 4.x / 5.x) fails.

### Step 4 — toplevel_removed reaches QML and clears focus

Close the window by its resolved PID (`$PID` from Step 2), NOT by
`pkill -f` on the command line. A process-pattern kill silently
matches nothing when the client's argv differs from the pattern (the
original failure mode: `Process info:` was empty at close time, the
window was never killed, yet removal was still asserted). If no PID
was resolved we cannot deterministically close the window, so the
step is classified as an ERROR (test/infra problem) rather than
asserting the removal of a window we never closed.

```bash
# No resolvable PID => cannot close deterministically. ERROR, not FAIL.
case "$PID" in
    ''|*[!0-9]*) echo "ERROR: Step 4 — could not resolve a PID for handle=$HANDLE from qdwin's toplevel_added log (cannot close the window deterministically)"; exit 1;;
esac

CURSOR=$("$QDWIN_VM_EXEC" "$VMNAME" "journalctl _UID=1000 -n 1 \
  --show-cursor --no-pager 2>/dev/null | tail -1 | sed 's/^-- cursor: //'")
echo "closing qd16-step2 pid=$PID (handle=$HANDLE)"
"$QDWIN_VM_EXEC" "$VMNAME" "kill -TERM $PID 2>/dev/null; sleep 0.5; kill -KILL $PID 2>/dev/null || true"

# Bounded wait for the compositor to log the teardown, instead of a fixed
# 2s sleep: poll for `toplevel_removed handle=$HANDLE` for up to ~10s.
removed=
for _ in $(seq 1 50); do
    "$QDWIN_VM_EXEC" "$VMNAME" \
        "journalctl _UID=1000 --after-cursor='$CURSOR' --no-pager | \
         grep -qE 'qdwin: toplevel_removed handle=$HANDLE'" && { removed=1; break; }
    sleep 0.2
done

# Fail loudly if the teardown was never observed, so a non-LLM runner
# cannot silently continue past an unremoved window.
[ -n "$removed" ] || { echo "FAIL: no 'qdwin: toplevel_removed handle=$HANDLE' within ~10s of killing pid=$PID"; exit 1; }
```

**Assert (4.1):** `qdwin: toplevel_removed handle=$HANDLE` after
`$CURSOR` (i.e. the bounded wait above set `removed=1`). If `removed`
is empty the window's teardown was never observed within ~10s of the
TERM/KILL close sequence.

**Assert (4.2):** `qdwin: seat_focus_changed seat=default
handle=4294967295` after `$CURSOR` — the focus drops to UINT32_MAX
because no toplevel remains. The compositor side runs the focus-
recovery idle (todo/qdwin-focus-events.md) and finds no candidate.

### Step 5 — Alt+Tab cycles focus via the protocol

Spawn two toplevels so Alt+Tab has somewhere to go. The shell's
`onSwitcherNext` + `onSwitcherCommit` handlers (added 2026-05-14
in `qdshell/Services/Qdwin/Qdwin.qml`) walk the windows ListModel
and call `qdwinBinding.focusWindow(handle)` on commit.

```bash
# Capture a cursor BEFORE spawning so we can HARD-GATE on both toplevels
# reaching qdwin — an ungated spawn would leave the Alt+Tab assertions
# depending on windows that may never have mapped (the original failure mode).
SPAWN_CURSOR=$("$QDWIN_VM_EXEC" "$VMNAME" "journalctl _UID=1000 -n 1 \
  --show-cursor --no-pager 2>/dev/null | tail -1 | sed 's/^-- cursor: //'")
"$QDWIN_VM_EXEC" "$VMNAME" \
    "setsid -f runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
     WAYLAND_DISPLAY=wayland-1 qdistro-test-window --title 'qd16-step5a' \
     --width 300 --height 180 --color 0xff304050 >/tmp/qd16-step5a.log 2>&1"
sleep 1
"$QDWIN_VM_EXEC" "$VMNAME" \
    "setsid -f runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
     WAYLAND_DISPLAY=wayland-1 qdistro-test-window --title 'qd16-step5b' \
     --width 300 --height 180 --color 0xff405060 >/tmp/qd16-step5b.log 2>&1"
# Both qdistro-test-window toplevels must reach qdwin before we exercise
# Alt+Tab; otherwise this is a precondition ERROR, not a switcher FAIL.
step5_ok=
for _ in $(seq 1 40); do
    n=$("$QDWIN_VM_EXEC" "$VMNAME" \
        "journalctl _UID=1000 --after-cursor='$SPAWN_CURSOR' --no-pager | \
         grep -cE 'qdwin: toplevel_added handle=[0-9]+ uid=1000 pid=[0-9]+ app_id=qdistro-test-window'")
    [ "${n:-0}" -ge 2 ] && { step5_ok=1; break; }
    sleep 0.2
done
[ -n "$step5_ok" ] || { echo "ERROR: Step 5 — fewer than 2 qdistro-test-window toplevels reached qdwin (got ${n:-0})"; exit 1; }
CURSOR=$("$QDWIN_VM_EXEC" "$VMNAME" "journalctl _UID=1000 -n 1 \
  --show-cursor --no-pager 2>/dev/null | tail -1 | sed 's/^-- cursor: //'")
qdwin_chord alt -- tab
sleep 1
```

**Assert (5.1):** at least one `qdwin: switcher_next dir=` line
after `$CURSOR` (qdwin saw the Tab while Alt was held and emitted
the protocol event to the bound shell).

**Assert (5.2):** at least one `qdwin: focus handle=` line after
`$CURSOR` — confirming the shell's `onSwitcherCommit` actually
called `set_keyboard_focus` and qdwin honored it. Before the
2026-05-14 alt+tab implementation in Qdwin.qml, qdwin would emit
`switcher_next` but the shell did nothing — the focus stayed put.

### Cleanup

```bash
"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -f "[q]distro-test-window" 2>/dev/null; true' >/dev/null
```

## Pass criteria

**Required (hard):** asserts 1.1, 1.2, 2.1, 2.2, 4.1, 4.2, 5.1, 5.2.
All must pass for overall PASS.

**Soft (non-blocking):** assert 3.1. OCR / visual bar-title check.
If 3.1 fails alone → overall **PASS**, status.txt = `PASS`, report
the 3.1 miss as `SOFT-FAIL` with a one-line diagnostic. Never map a
3.1-only miss to overall FAIL (Luna agents historically over-failed
here when host tesseract was missing).

## Known-broken-if

- 3.1 soft-fails with blank bar text while 2.x/4.x/5.x pass:
  guest fontconfig has no real faces (FontService logs
  `Loaded 1 fonts, 1 monospace`). Setup's font precondition should
  install dejavu/liberation; if zypper is offline that install is
  skipped and 3.1 stays soft. Not a binding regression.
- 1.1 silent: qdwin saw a registry global_bind but no
  bind_as_shell follow-up. Plugin loaded, called registry_bind
  but raced/segfaulted before bind_as_shell. Check qs stderr for
  segfault.
- 1.2 silent: plugin .so isn't on QML_IMPORT_PATH (check the
  qdshell.service Environment), OR the plugin loaded but
  the QdwinBinding constructor errored before `hello`. Check
  for `qdwin-binding: error:` lines in journal.
- 5.1 silent + 5.2 silent: qdwin's switcher_grab never entered.
  QMP `alt down → tab tap → alt up` injection didn't reach the
  keyboard focus owner. Compare against
  `t_focus_event_on_alt_tab` in `gui-regression-tests.sh` which
  uses the same injection path.
- 5.1 fires but 5.2 silent: the binding is up but Qdwin.qml's
  `onSwitcherCommit` didn't act. Inspect `Services/Qdwin/Qdwin.qml`
  for the alt+tab cycle implementation.

## Why agent-driven

The journal-line asserts are tractable from a plain shell
runner, but step 3 (bar OCR) and the cross-correlation between
qdwin's view of `toplevel_*` events and qdshell's view of its
`windows` ListModel benefit from an LLM able to inspect QML state
via screenshot + diff and decide if the surfaces actually
correspond. A subagent invoked through `gui-regression-tests.sh`'s
`t_agent_explores_*` pattern is the right runner: it can spawn
windows in varying orders, sample the bar, and report mismatches.
