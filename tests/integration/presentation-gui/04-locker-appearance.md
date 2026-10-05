# 04 — the locker freezes its appearance while locked, survives a shell kill

<!-- qci:visual: required -->
<!-- qci:visual-captures: 4 -->

**Acceptance criterion (plan 01 "Other surfaces", locker; 09 item 11):**
qdlocker reads the managed snapshot when it locks and freezes it for the
whole lock: a shell dark→light switch while locked does NOT restyle the lock
screen, and killing qdshell while locked neither unlocks nor restyles it.
After unlocking, the NEXT lock uses the new (light) appearance. Locking is
never delayed by appearance work.

Machine oracles: `qdlocker_ctrl status` (`locked=True/False`),
`qdlocker_ctrl unlock-result`, `pres_snapshot`. Visual oracle: the lock UI
(clock, date, Password prompt) colours in the frames.

## Setup

```bash
source "${QDISTRO_REPO}/tests/integration/presentation-gui/presentation-helpers.sh"
source "${QDLOCKER_REPO}/tests/gui/qdlocker-helpers.sh"
qdwin_set_vm "${VMNAME:?qci sets VMNAME to this scenario's VM}"
qdlocker_session_healthy || { echo "FAIL: session not up"; exit 2; }
case "$(qdlocker_ctrl status 2>/dev/null)" in
    *locked=True*) qdlocker_unlock_with_password; qdlocker_wait_for_unlock 5 ;;
esac
pres_kill_apps
pres_qs_ipc darkMode setDark
pres_wait_mode dark 30 || { echo "FAIL: shell did not publish dark"; exit 1; }
```

## Steps

### Step 1 — lock under the dark snapshot

```bash
qdwin_chord ctrl alt -- l
qdlocker_wait_for_lock 5
qdlocker_ctrl status
qdwin_screenshot /tmp/pres04-step1-locked-dark.png
```

**Assert (1.1):** status reports `locked=True`.
**Assert (1.2):** OPEN the frame: the lock UI (clock, date, Password prompt)
is drawn on a DARK background.

### Step 2 — shell switches to light while locked; then the shell is killed

```bash
pres_qs_ipc darkMode setLight
SNAP_LIGHT=$(pres_wait_mode light 30); echo "SNAP_LIGHT=$SNAP_LIGHT"
sleep 3
qdwin_screenshot /tmp/pres04-step2-locked-after-light.png
pres_admin "systemctl --user kill --signal=KILL qdshell.service"
sleep 3
qdlocker_ctrl status
qdwin_screenshot /tmp/pres04-step2-locked-after-shell-kill.png
```

`qdwin_screenshot` is served by qdshell and waits for its respawn after the
kill (`WARN: capture-after-shell-restart` is expected).

**Assert (2.1):** `SNAP_LIGHT` mode is `light` (the snapshot did change).
**Assert (2.2):** OPEN `pres04-step2-locked-after-light.png`: the lock UI is
still DARK — it did not restyle while locked.
**Assert (2.3):** status still reports `locked=True` after the shell kill.
**Assert (2.4):** OPEN `pres04-step2-locked-after-shell-kill.png`: the lock
UI is still shown and still DARK.

### Step 3 — unlock, then lock again: the new lock is light

```bash
qdlocker_unlock_with_password
qdlocker_wait_for_unlock 5
qdlocker_ctrl unlock-result
qdwin_chord ctrl alt -- l
qdlocker_wait_for_lock 5
qdwin_screenshot /tmp/pres04-step3-relocked-light.png
qdlocker_unlock_with_password
qdlocker_wait_for_unlock 5
qdlocker_ctrl status
```

**Assert (3.1):** `unlock-result` reports `last=success`; the final status
is `locked=False`.
**Assert (3.2):** OPEN the Step 3 frame: the lock UI is drawn on a LIGHT
background with readable dark text.

## Cleanup

```bash
case "$(qdlocker_ctrl status 2>/dev/null)" in
    *locked=True*) qdlocker_unlock_with_password; qdlocker_wait_for_unlock 5 ;;
esac
pres_qs_ipc darkMode setDark >/dev/null
```

## Pass criteria

All asserts 1.1 → 3.2 pass.

## Known-broken-if

- 2.2 light while still locked: the locker watches the snapshot during a
  lock instead of freezing it (`_schedule_presentation_freeze`).
- 2.3 `locked=False`: a shell death unlocked the session (see
  `qdlocker/tests/gui/06-shell-crash-survives.md`).
- 3.2 dark: the locker never re-reads after unlock (a stale freeze).
