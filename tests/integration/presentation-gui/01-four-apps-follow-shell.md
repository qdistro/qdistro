# 01 — four apps follow the shell's appearance live, survive a shell kill

<!-- qci:visual: required -->
<!-- qci:visual-captures: 9 -->

**Acceptance criterion (pack 07 scenarios 1 and 2, 100% scale):** the four
first-party apps (qfileman, qterminator, qdbrowser, qnotebook), started
while qdshell publishes **dark**, show dark chrome. When the shell switches
to **light**, every running app — including an open Settings dialog —
restyles to light **without relaunch** (same PIDs). Killing qdshell does not
exit, block or restyle any app; the snapshot file keeps the last
generation. A NEW app window started while the shell is down reads the
persisted (light) snapshot. When the respawned shell switches back to dark,
the running apps follow again.

Machine oracles (exact): `pres_snapshot` generation/mode, `pres_app_pids`.
Visual oracle: each app's window chrome (menu bar / toolbar / side panel /
title area background) in the frame you just opened. Page content of
qdbrowser (`about:blank`, the web view) is CONTENT, not chrome: it does not
have to follow and must not be graded as chrome.

## Setup

```bash
source "${QDISTRO_REPO}/tests/integration/presentation-gui/presentation-helpers.sh"
qdwin_set_vm "${VMNAME:?qci sets VMNAME to this scenario's VM}"
qdwin_session_healthy || { echo "FAIL: qdwin session not healthy"; exit 2; }
pres_kill_apps
pres_qs_ipc darkMode setDark
pres_wait_mode dark 30 || { echo "FAIL: shell did not publish dark"; exit 1; }
SNAP_DARK=$(pres_snapshot); echo "SNAP_DARK=$SNAP_DARK"
```

## Steps

### Step 1 — start the four apps under the dark snapshot

```bash
pres_launch qfm   qfileman
pres_launch qterm qterminator
pres_launch qdb   "python3 -m qdbrowser --no-restore about:blank"
pres_launch qnb   qnotebook
sleep 10
PIDS_1=$(pres_app_pids); echo "PIDS_1=$PIDS_1"
for app in qfileman qterminator qdbrowser qnotebook; do
    pres_focus_app "$app" && sleep 1.5
    qdwin_screenshot "/tmp/pres01-step1-$app.png"
done
```

**Assert (1.1):** `PIDS_1` has a numeric pid for all four apps (no `none`).
**Assert (1.2):** OPEN each of the four Step 1 frames. In each, the focused
app (its name is in the bar's window-title chip) is the window in front and
its chrome is DARK (dark background, light text). If a frame shows a
different app in front than the bar names, record that in your notes as a
focus/raise observation and grade only what is visible; a frame whose app
window is not visible at all is ERROR for that app, not FAIL.

### Step 2 — open qnotebook's Settings dialog

```bash
pres_focus_app qnotebook; sleep 1
qdwin_chord ctrl -- comma
sleep 2
qdwin_screenshot /tmp/pres01-step2-qnotebook-settings.png
```

**Assert (2.1):** the frame shows a qnotebook Settings dialog with dark
chrome. If no dialog appeared, note it and continue (2.x is then ERROR, not
FAIL — the shortcut is the only thing under test there).

### Step 3 — the shell switches to light; running apps follow

```bash
pres_qs_ipc darkMode setLight
SNAP_LIGHT=$(pres_wait_mode light 30) || echo "FAIL: shell did not publish light"
echo "SNAP_LIGHT=$SNAP_LIGHT"
sleep 3
PIDS_3=$(pres_app_pids); echo "PIDS_3=$PIDS_3"
qdwin_screenshot /tmp/pres01-step3-settings-light.png
for app in qfileman qterminator qdbrowser; do
    pres_focus_app "$app" && sleep 1.5
    qdwin_screenshot "/tmp/pres01-step3-$app.png"
done
```

**Assert (3.1):** `SNAP_LIGHT` mode is `light` and its generation differs
from `SNAP_DARK`'s.
**Assert (3.2):** `PIDS_3` equals `PIDS_1` exactly (no app relaunched).
**Assert (3.3):** OPEN `pres01-step3-settings-light.png`: the still-open
qnotebook Settings dialog (and the qnotebook window behind it, where
visible) now has LIGHT chrome.
**Assert (3.4):** OPEN the three per-app Step 3 frames: each app's chrome is
LIGHT.

### Step 4 — kill qdshell; apps keep running and keep light

```bash
pres_admin "systemctl --user kill --signal=KILL qdshell.service"
sleep 3
PIDS_4=$(pres_app_pids); echo "PIDS_4=$PIDS_4"
SNAP_4=$(pres_snapshot); echo "SNAP_4=$SNAP_4"
qdwin_screenshot /tmp/pres01-step4-after-shell-kill.png
```

`qdwin_screenshot` is served by qdshell, so it waits for the unit's
`Restart=on-failure` respawn and may print `WARN: capture-after-shell-restart`
— expected here.

**Assert (4.1):** `PIDS_4` equals `PIDS_1`.
**Assert (4.2):** `SNAP_4` equals `SNAP_LIGHT` (a shell crash does not
rewrite or delete the snapshot).
**Assert (4.3):** OPEN the Step 4 frame: the app window in front still has
LIGHT chrome.

### Step 5 — a new app window joins with the persisted snapshot

```bash
pres_launch qterm2 "qterminator --new-window"
sleep 6
qdwin_screenshot /tmp/pres01-step5-new-window.png
```

**Assert (5.1):** OPEN the Step 5 frame: the newly opened terminal window is
in front and its chrome is LIGHT.

### Step 6 — the respawned shell switches back to dark; apps follow

```bash
pres_qs_ipc darkMode setDark
SNAP_DARK2=$(pres_wait_mode dark 30) || echo "FAIL: respawned shell did not publish dark"
echo "SNAP_DARK2=$SNAP_DARK2"
sleep 3
PIDS_6=$(pres_app_pids); echo "PIDS_6=$PIDS_6"
pres_focus_app qfileman && sleep 1.5
qdwin_screenshot /tmp/pres01-step6-qfileman-dark.png
```

**Assert (6.1):** `SNAP_DARK2` mode is `dark`; `PIDS_6` equals `PIDS_1`.
**Assert (6.2):** OPEN the Step 6 frame: qfileman's chrome is DARK again.

## Cleanup

```bash
pres_kill_apps
pres_qs_ipc darkMode setDark >/dev/null
```

## Pass criteria

Every assert 1.1 → 6.2 passes. Machine asserts (1.1, 3.1, 3.2, 4.1, 4.2,
6.1) are decided by the printed values, not by pixels.

## Known-broken-if

- 3.2/4.1 FAIL (PIDs changed): an app restarted or crashed on a snapshot
  change or shell exit — see the app's `/tmp/pres-<tag>.log` in the guest.
- 3.4 FAIL for one app only: that app's `apply_presentation_update` path
  misses widgets; compare with the host four-app probe
  (`tests/integration/vm/presentation-four-apps.bats`).
- 4.2 FAIL: something deletes or rewrites `current.json` when qdshell dies.
- 5.1 FAIL (dark): a fresh process does not read the persisted managed
  snapshot (check `/usr/share/qdistro/presentation/deployment.json`).
