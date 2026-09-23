# 06 — qdshell crashing while locked does NOT unlock the screen

<!-- qci:visual: required -->

**Acceptance criterion (lifecycle independence):** the locker's
process is a peer of qdshell, not a child. If qdshell crashes while
the screen is locked:

1. The lock surface stays composited (qdwin keeps rendering it on
   the LOCK layer).
2. qdlocker keeps receiving `overlay_key` events.
3. Auth still succeeds.
4. qdshell comes back. systemd (`Restart=on-failure` on the deployed
   unit) respawns it after the KILL; a respawn that lands while the
   screen is still locked must SURVIVE its startup config push (qdwin
   drops, rather than fatally rejects, the locked-gated
   `set_pointer_config`/`set_key_repeat` snapshots), so the shell is
   alive and functional after unlock.

This is the regression test for the architectural decision to split
qdlocker out of qdshell. If a shell crash drops the screen unlocked,
the split bought us nothing.

## Setup

```bash
source "$(dirname "$0")/qdlocker-helpers.sh"
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"
qdlocker_session_healthy || { echo "FAIL: session not up"; exit 2; }

case "$(qdlocker_ctrl status 2>/dev/null)" in
    *locked=True*)
        "$QDWIN_VM_EXEC" "$VMNAME" \
          'runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart qdlocker.service; sleep 2' \
          >/dev/null
        ;;
esac
```

## Steps

### Step 1 — engage the locker

```bash
qdwin_chord ctrl alt -- l
qdlocker_wait_for_lock 5
qdwin_screenshot /tmp/qdlocker-06-step1-locked.png
```

**Assert (1.1):** `qdlocker_ctrl status` reports `locked=True`.

### Step 2 — kill qdshell

```bash
"$QDWIN_VM_EXEC" "$VMNAME" \
  'runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user kill --signal=KILL qdshell.service'
sleep 2
qdwin_screenshot /tmp/qdlocker-06-step2-shell-dead.png
qdlocker_ctrl status
```

The capture client behind `qdwin_screenshot` IS qdshell, so this frame cannot
be taken while the shell is literally dead: the helper waits (bounded) for
the unit's `Restart=on-failure` respawn to answer on its ctrl socket and to
map its wallpaper, then captures, and prints
`WARN: capture-after-shell-restart`. That is the expected path here, not a
defect — the frame still proves the point of 2.2, because the respawned shell
comes back while the screen is LOCKED and must not have displaced the lock
UI. (Before 2026-09-23 the helper's `socat` gave up 0.5s after sending the
request, so a capture served by a still-starting shell came back EMPTY and
this step was recorded ERROR; see the `-t` note in qdwin-helpers.sh.)

**Assert (2.1):** `qdlocker_ctrl status` still reports
`locked=True`. The shell death did not affect the locker's process
or the compositor's lock state.
**Assert (2.2):** screenshot still shows the qdlocker UI (clock, date,
`Password` prompt box, and the "capture monitoring" banner along the top —
that banner is part of the LOCK UI, not the qdshell bar). OPEN THIS FILE
itself before grading it; do not infer it from the Step 1 or Step 4 frame.
While locked the shell draws nothing visible, so the Step 2 frame normally
looks the same as Step 1 and is often BYTE-IDENTICAL to it (same clock
minute) — identical is the expected PASS shape, not a sign of a stale
capture (a stale capture is flagged by the helper's `WARN: stale-capture`).
In two 2026-09-23 verification runs the runner opened only one image and
graded this frame "black with only the panel" while the file showed the
full lock UI. The LOCK
layer is owned by qdwin from the locker's wl_surface — the shell's
death doesn't tear it down. Chrome / panel may be absent (shell is
dead, no decorations) but the lock UI is intact.

### Step 3 — type the password into the still-locked screen

```bash
qdlocker_type_password_chars
sleep 0.3
qdlocker_assert_prompt_len 11
```

**Assert (3.1):** `prompt-len=11`. The keyboard grab and overlay_key
forwarding survive the shell death.

### Step 4 — unlock

```bash
qdwin_send_key KEY_ENTER
qdlocker_wait_for_unlock 5
qdlocker_ctrl status
qdlocker_ctrl unlock-result
qdwin_screenshot /tmp/qdlocker-06-step4-unlocked.png
```

**Assert (4.1):** `qdlocker_ctrl unlock-result` reports
`last=success`; `qdlocker_ctrl status` reports `locked=False`.
**Assert (4.2):** `systemctl --user is-active qdshell.service` reports
`active`.
**Assert (4.3):** the Step 4 screenshot shows the unlocked DESKTOP drawn by
the respawned qdshell — at least its top bar — not the lock UI and not an
all-black frame. An all-black post-unlock frame here is a PRODUCT failure,
not a capture artefact: until qdwin 2026-09-23 every surface the respawned
shell mapped WHILE LOCKED (wallpaper, bar, background) stayed unmapped after
unlock, because qdwin re-positioned the hidden layers without re-mapping the
views inserted into them during the lock — the desktop stayed black, and
this step recorded it as "supporting evidence only" in every run from
2026-09-17 to 2026-09-22. If the helper reports the frame STALE
(`WARN: stale-capture`, `.meta` sidecar) it is not post-unlock evidence:
record ERROR for 4.3, not FAIL.

### Step 5 — qdshell is fully functional post-recovery

```bash
# Capture through a regular FILE, never a bare host-side `2>&1`.
#
# IF the tool running this block reads the command through a pipe -- which is
# the usual shape, though this block cannot prove your tool's descriptor
# topology -- then `2>&1` puts vm-exec's fd 2 on that pipe, and the read
# finishes only when the LAST WRITER closes it, not when vm-exec exits. A
# HOST-side descendant of vm-exec (virsh, jq) that outlives it therefore holds
# the reader open and no outer timeout helps. Note the descendant must be a
# host process: the guest agent runs in the GUEST and never holds a host
# descriptor.
#
# The path is unique per run and the replay is byte-bounded, so a survivor of
# an earlier attempt cannot contaminate this one and the replay cannot chase a
# growing file. (This is weaker than qdwin_vmx_merged, which unlinks the
# capture before the command starts; here the file is named while it is
# written.)
_qdl_cap=$(mktemp /tmp/qdlocker-06-step5.XXXXXXXX)
"$QDWIN_VM_EXEC" "$VMNAME" 'runuser -l admin -c "systemctl --user is-active qdshell.service"' \
  > "$_qdl_cap" 2>&1
head -c 65536 "$_qdl_cap"
rm -f "$_qdl_cap"
"$QDWIN_VM_EXEC" "$VMNAME" \
  'runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 qs ipc -p /usr/share/quickshell/qdshell call qdwin capabilities'
```

**Assert (5.1):** systemd reports `active` and Quickshell IPC answers
the `qdwin capabilities` call. qdshell came back online via its
unit's `Restart=on-failure`.

## Cleanup

```bash
true
```

## Pass criteria

All asserts 1.1 → 5.1 pass. Confirms the lifecycle independence:
neither process owns the other.

## Known-broken-if

- Step 2 FAIL with `locked=False` after killing qdshell — qdwin's
  lock state is tied to the shell binding instead of the locker
  binding. This would mean `bind_qdwin_shell` destroy handler is
  calling `set_locked(0)`; it must not.
- Step 2 screenshot shows a black screen with no lock UI — qdwin
  unmapped the lock surface when the shell disconnected. The lock
  surface must be owned by the locker resource, not the shell. The
  C-side reorganization in `qdwin/doc/locker.md §1` is the fix.
- Step 3 PASS at `prompt-len=11` but Step 4 FAIL — PAM authentication
  needs the seat/session, which logind might tear down when the
  shell exits. Check `loginctl list-sessions` inside the VM; if the
  admin session is gone, the shell's
  `Restart=on-failure` plus its own logind activation should bring it
  back, but a transient PAM failure is possible. Re-run after the
  retry path is wired in `auth.py`.
- Step 5 FAIL with `Result=start-limit-hit`, `NRestarts>=5`, and a
  Quickshell crash-loop logging `wl_display_flush ... Broken pipe` —
  qdwin is fatally rejecting the restarted shell's startup
  `set_pointer_config`/`set_key_repeat` while locked (a
  `wl_resource_post_error(LOCKED)` instead of a logged drop), so every
  respawn during the lock dies and burns the unit's
  `StartLimitBurst=5/30s`, leaving the desktop dead even after unlock.
  The locked gate for those two session-config snapshots must refuse
  by DROPPING the request, never by posting a fatal protocol error.
- Step 4 reports `locked=False`, Step 5 IPC answers, but the Step 4
  screenshot is black (bar and wallpaper absent) — the respawned shell's
  layer surfaces mapped while locked were never re-mapped on unlock. qdwin's
  `qdwin_show_non_lock_layers()` must re-map views that
  `weston_view_move_to_layer()` inserted into the then-unpositioned layers
  (`qdwin_layer_remap_after_unlock`), and damage the outputs.
- Step 4 reports `locked=False` but screenshot still shows lock UI —
  qdwin destroyed the lock_surface resource but did not flip the
  compositor state machine. B1-style bug; see qdwin's 03-locker-cycle
  §"Known-broken-if".
