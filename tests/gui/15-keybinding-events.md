# 15 — qdwin keybinding events emit journal log lines

<!-- qci:visual: none -->

**Acceptance criterion:** every keybinding handled by the compositor
(Ctrl+Space launcher, Alt+Tab switcher, Ctrl+Alt+L lock, registered
hotkeys, overlay-grab keys) emits a `qdwin: <event>` log line
**independent of qdshell binding state**. Without this, the
keybinding code paths silently drop input on a missing/buggy shell
and the silent-drop bug class (the suite's motivating problem)
escapes detection. See
`todo/qdwin-keybindings-uninstrumented.md` for the diagnosis and
the 2026-05-14 fix.

## Setup

```bash
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh
if [ -f "${QDLOCKER_REPO:-${QDWIN_REPO}/../qdlocker}/tests/gui/qdlocker-helpers.sh" ]; then
    # shellcheck disable=SC1090
    source "${QDLOCKER_REPO:-${QDWIN_REPO}/../qdlocker}/tests/gui/qdlocker-helpers.sh"
fi
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"

pgrep -f "http.server 8765" >/dev/null || (
    cd ${QDWIN_REPO} && \
    python3 -m http.server 8765 --bind 127.0.0.1 >/tmp/qdistro-http.log 2>&1 &
)
sleep 1
qdwin_session_healthy || { echo "FAIL: session not up"; exit 1; }

# A previous qdlocker scenario can leave the compositor in a real locked
# state. In that state global keybindings are intentionally suppressed and
# the keyboard is routed to the locker as `overlay_key role=2`, which is not
# a failure of the keybinding instrumentation this scenario is trying to
# exercise.
if command -v qdlocker_drain_lock_state >/dev/null 2>&1; then
    qdlocker_drain_lock_state || { echo "FAIL: could not drain stale qdlocker lock state"; exit 1; }
else
    case "$("$QDWIN_VM_EXEC" "$VMNAME" 'printf "status\n" | runuser -u admin -- socat -t 1 - UNIX-CONNECT:/run/user/1000/qdlocker.sock 2>/dev/null || true')" in
        *locked=True*)
            "$QDWIN_VM_EXEC" "$VMNAME" \
              'runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart qdlocker.service; sleep 2' \
              >/dev/null
            ;;
    esac
fi

"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -f "[f]oot|[w]eston-terminal|[q]distro-test-window" 2>/dev/null; sleep 1' >/dev/null
```

## Steps

### Step 1 — Ctrl+Space drives launcher_requested

```bash
CURSOR=$("$QDWIN_VM_EXEC" "$VMNAME" "journalctl _UID=1000 -n 1 \
  --show-cursor --no-pager 2>/dev/null | tail -1 | sed 's/^-- cursor: //'")
qdwin_chord ctrl -- spc
sleep 0.5
qdwin_send_key KEY_ESC                          # dismiss launcher
sleep 0.3
"$QDWIN_VM_EXEC" "$VMNAME" \
  "journalctl _UID=1000 --after-cursor='$CURSOR' --no-pager | \
   grep -E 'qdwin: launcher_requested'"
```

**Assert (1.1):** at least one `qdwin: launcher_requested` log line.

**Assert (1.2):** if qdshell is running and bound at v>=1 the launcher
overlay is briefly visible — capture a screenshot mid-chord
(`qdwin_screenshot /tmp/15-step1-launcher.png`) and check for the
launcher's distinctive surface (`grep launcher` in journal works as
a proxy if the screenshot is ambiguous).

### Step 2 — Alt+Tab drives switcher_next + switcher_commit

```bash
# Need two windows for the switcher to do meaningful work.
for i in 1 2; do
    "$QDWIN_VM_EXEC" "$VMNAME" '
      if command -v qdistro-test-window >/dev/null 2>&1; then
        setsid -f runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
          qdistro-test-window --title "qd15-switch-'"$i"'" --width 320 --height 200 >/tmp/qd15-switch-'"$i"'.log 2>&1
      elif command -v weston-terminal >/dev/null 2>&1; then
        setsid -f runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
          weston-terminal >/tmp/qd15-switch-'"$i"'.log 2>&1
      else
        echo "ERROR: no qdistro-test-window or weston-terminal available"; exit 1
      fi' >/dev/null
    sleep 1
done
CURSOR2=$("$QDWIN_VM_EXEC" "$VMNAME" "journalctl _UID=1000 -n 1 \
  --show-cursor --no-pager 2>/dev/null | tail -1 | sed 's/^-- cursor: //'")
qdwin_chord alt -- tab
sleep 0.5
"$QDWIN_VM_EXEC" "$VMNAME" \
  "journalctl _UID=1000 --after-cursor='$CURSOR2' --no-pager | \
   grep -E 'qdwin: switcher_(next|commit)'"
```

**Assert (2.1):** at least one `qdwin: switcher_next dir=1` line
(forward direction) AND at least one `qdwin: switcher_commit cause=…`
line on Alt-release. The `cause=` tag distinguishes the
mod-fallback / alt-released / non-tab-key code paths.

### Step 3 — Ctrl+Alt+L drives lock_requested or bound-shell warning

```bash
CURSOR3=$("$QDWIN_VM_EXEC" "$VMNAME" "journalctl _UID=1000 -n 1 \
  --show-cursor --no-pager 2>/dev/null | tail -1 | sed 's/^-- cursor: //'")
qdwin_chord ctrl alt -- l
sleep 0.6
"$QDWIN_VM_EXEC" "$VMNAME" \
  "journalctl _UID=1000 --after-cursor='$CURSOR3' --no-pager | \
   grep -E 'qdwin: lock_requested|qdwin: lock key pressed'"
```

**Assert (3.1):** exactly one of:

- `qdwin: lock_requested` (shell bound at v>=7, normal path).
- `qdwin: lock key pressed; no shell bound` (no shell, log-only).
- `qdwin: lock key pressed but shell bound <v7` (old-version shell).

The exact line indicates which branch fired; ALL three are valid
log lines and ALL prove the keybinding was processed by the
compositor (the canonical silent-drop motivating bug would emit
none of them).

### Step 4 — Hotkey registered via shell fires hotkey_pressed

A registered hotkey (via `qdwin_shell_v1.register_hotkey`) should
emit `qdwin: hotkey_pressed id=N` on each press. This requires a
shell-bound client to register the hotkey first; the
qdwin-bystander test client can do this when run with
`--register-hotkey <id> <mods> <key>`. Skip this step if
qdwin-bystander isn't built or the shell isn't a test client.

```bash
# (Optional — only if running with qdwin-bystander as shell.)
# qdwin_send_key KEY_PRINT
# journalctl ... | grep "qdwin: hotkey_pressed id="
```

**Assert (4.1):** `qdwin: hotkey_pressed id=<expected>` on the
keystroke. **Skipped when no registering shell is present.** This is
an OPTIONAL assertion: skipping it never changes the scenario verdict
(see Pass criteria).

## Cleanup

```bash
"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -x foot 2>/dev/null; true' >/dev/null
"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -f "[w]eston-terminal|[q]distro-test-window" 2>/dev/null; true' >/dev/null

# Step 3 really LOCKS the screen. Leaving it locked poisons everything after it:
# with the locker grab active, qdwin routes the keyboard to the locker as
# `overlay_key role=2` and the keybinding lines this scenario greps for stop
# appearing — so a re-attempt, or the next scenario on a shared session VM,
# observes nothing and reads as broken instrumentation. That is what turned this
# scenario into an ERROR in full-20260914T194046Z-13620 ("the guest remained in
# the locker state after the stale-lock recovery attempt"). Undo the lock with
# the SAME drain Setup uses, so the scenario leaves the session as it found it.
#
# This is a REQUIRED assertion (C.1), not housekeeping. A warning here would let
# the scenario report PASS while handing the next scenario on a shared session VM
# a session that only routes input to the locker — the exact poison this block
# exists to remove. Failure to restore the session is a failure of the scenario.
QD15_CLEANUP_FAILED=0

if command -v qdlocker_drain_lock_state >/dev/null 2>&1; then
    qdlocker_drain_lock_state \
        || { echo "FAIL (C.1): cleanup could not drain the lock state"; QD15_CLEANUP_FAILED=1; }
else
    case "$("$QDWIN_VM_EXEC" "$VMNAME" 'printf "status\n" | runuser -u admin -- socat -t 1 - UNIX-CONNECT:/run/user/1000/qdlocker.sock 2>/dev/null || true')" in
        *locked=True*)
            "$QDWIN_VM_EXEC" "$VMNAME" \
              'runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart qdlocker.service; sleep 2' \
              >/dev/null \
              || { echo "FAIL (C.1): cleanup could not restart qdlocker"; QD15_CLEANUP_FAILED=1; }
            ;;
    esac
fi

# VERIFY the end state; do not infer it from the drain's exit code. Neither
# recovery path proves an unlock on its own:
#   - `qdlocker_drain_lock_state` returns 0 when its `status` read yields
#     anything that is not `locked=True` — including an EMPTY read from an
#     unreachable ctrl socket, because its `case` then matches nothing and falls
#     through with success;
#   - the fallback branch above restarts qdlocker and returns whatever the
#     restart returned, which says nothing about the resulting lock state.
# So read the locker's own introspection back and require `locked=False`.
QD15_LOCK_STATE=
for _ in $(seq 1 20); do
    QD15_LOCK_STATE=$("$QDWIN_VM_EXEC" "$VMNAME" \
        'printf "status\n" | runuser -u admin -- socat -t 2 - UNIX-CONNECT:/run/user/1000/qdlocker.sock 2>/dev/null || true')
    case "$QD15_LOCK_STATE" in *locked=*) break ;; esac
    sleep 0.5
done

case "$QD15_LOCK_STATE" in
    *locked=False*)
        echo "C.1 ok: session left UNLOCKED (locked=False)"
        ;;
    *locked=True*)
        echo "FAIL (C.1): the session is STILL LOCKED after cleanup — every"
        echo "      scenario that follows on this VM will see only"
        echo "      'qdwin: overlay_key role=2'. Unlock it by hand before reusing"
        echo "      the session: qdlocker_drain_lock_state, or restart"
        echo "      qdwin-compositor.service + qdshell.service + qdlocker.service."
        QD15_CLEANUP_FAILED=1
        ;;
    *)
        # An unreadable ctrl socket is NOT evidence of an unlocked session: a
        # locker that died while the compositor was locked leaves the fail-secure
        # blank in place (qdwin/test_lock_fail_secure.py) and looks exactly like
        # this. The one case where it is genuinely inapplicable is an image with
        # no locker at all — which is checked, not assumed.
        if "$QDWIN_VM_EXEC" "$VMNAME" \
             'runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user show qdlocker.service -p LoadState --value 2>/dev/null' \
           | grep -q not-found; then
            echo "C.1 n/a: no qdlocker.service on this image, so Ctrl+Alt+L cannot have locked the session"
        else
            echo "FAIL (C.1): qdlocker is installed but its ctrl socket did not"
            echo "      answer within 10s, so the unlocked state is UNVERIFIED."
            echo "      Treat the session as poisoned: check it by hand before"
            echo "      running anything else on this VM."
            QD15_CLEANUP_FAILED=1
        fi
        ;;
esac

if [ "$QD15_CLEANUP_FAILED" != 0 ]; then
    echo "SCENARIO VERDICT: FAIL (cleanup C.1 — see the FAIL (C.1) lines above)"
    exit 1
fi
```

## Pass criteria

Asserts 1.1, 2.1, 3.1 and the cleanup assertion C.1 are the REQUIRED
assertions; 4.1 is conditional on test-shell presence.

**C.1 — the session is left unlocked.** Step 3 really locks the screen, and
Cleanup must put it back. C.1 is satisfied only by reading `locked=False` back
out of qdlocker's ctrl socket (or by establishing that the image carries no
qdlocker at all). A cleanup that cannot demonstrate that is a **FAIL**, not a
warning: the next scenario sharing this session VM would receive a session that
routes every keystroke to the locker, and this scenario must not report PASS
while leaving that behind.

**Verdict rule.** The scenario verdict is decided by the required
assertions alone:

- 1.1, 2.1, 3.1 and C.1 all pass -> **PASS**, even though 4.1 was skipped.
  Record 4.1 as `SKIP` in the per-assertion list and name the reason
  (no registering shell client). "Some steps skipped, none failed" is
  a PASS. It is NOT an ERROR, and it is NOT a whole-scenario SKIP
  either — the scenario ran.
- any of 1.1, 2.1, 3.1 or C.1 fails -> **FAIL**. A C.1 failure is a FAIL even
  when 1.1, 2.1 and 3.1 all passed: the steps proved the instrumentation, and
  the scenario still left the VM in a state the next one cannot use. Say so
  explicitly in the verdict, and say whether the session is still locked.
- you could not observe 1.1, 2.1 or 3.1 at all (no journal cursor, the
  session never came up, the screen stayed locked so every step saw only
  `qdwin: overlay_key role=2`) -> **ERROR**. Say which required assertion
  was unobservable and why.

Confirms the 2026-05-14 keybinding-instrumentation fix
(`qdwin/qdwin.c`) is wired correctly across all four code paths.

## Known-broken-if

- 1.1 silent: `qdwin_handle_launcher_key` (or the
  `qdwin_shell_v1_send_launcher_requested` site) is missing the
  preceding `weston_log` call.
- 2.1 missing `switcher_next` but `switcher_commit` present: the
  switcher_grab tab-handling branch is reaching the send but
  bypassing the log (look at the `if (key == KEY_TAB)` block in
  `qdwin_switcher_grab_key`).
- 2.1 missing both: weston modifier_binding never fired — confirm
  you used `qdwin_chord alt -- tab` (QMP), NOT
  `qdwin_send_key KEY_LEFTALT KEY_TAB` (virsh send-key). See the
  AGENTS.md "Why two key paths" post-mortem.
- all steps show only `qdwin: overlay_key role=2`: the screen is locked
  and the locker grab is active. The setup drain failed or was skipped;
  fix the stale lock state before blaming qdwin keybindings.
- 3.1 silent: `qdwin_handle_lock_key` reached but didn't reach any
  log branch — gate inversion somewhere.
