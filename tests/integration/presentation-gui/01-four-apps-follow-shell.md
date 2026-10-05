# 01 — four apps follow the shell's appearance live, survive a shell kill

<!-- qci:visual: required -->
<!-- qci:visual-captures: 13 -->

**Acceptance criterion (pack 07 scenarios 1 and 2, 100% scale):** the four
first-party apps (qfileman, qterminator, qdbrowser, qnotebook), started
while qdshell publishes **dark**, show dark chrome. When the shell switches
to **light**, every running app — including an open Settings dialog —
restyles to light **without relaunch** (same PIDs). Killing qdshell does not
exit, block or restyle any app; the snapshot file keeps the last
generation. A NEW app started while the shell is stopped reads the
persisted (light) snapshot. When the restarted shell switches back to dark,
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

Window stacking: qdwin opens these apps maximized, newest on top, and a
`focusWindow` IPC focuses a window without necessarily RAISING it. The
steps therefore never rely on raising: each app is captured right after it
starts (it is on top), and after the mode switch the apps are revealed one
by one by CLOSING the one above. Every app is still running when the shell
switches; only the reveal closes them, after their PIDs were checked.

### Step 1 — start the four apps under the dark snapshot, one at a time

```bash
for spec in "qfm:qfileman" "qterm:qterminator" "qdb:python3 -m qdbrowser --no-restore about:blank" "qnb:qnotebook"; do
    tag=${spec%%:*}; cmd=${spec#*:}
    pres_launch "$tag" "$cmd"
    sleep 7
    qdwin_screenshot "/tmp/pres01-step1-$tag.png"
done
PIDS_1=$(pres_app_pids); echo "PIDS_1=$PIDS_1"
```

**Assert (1.1):** `PIDS_1` has a numeric pid for all four apps (no `none`).
**Assert (1.2):** OPEN each of the four Step 1 frames. Each shows the app
just started (qfileman, qterminator, qdbrowser, qnotebook in that order) in
front, with DARK chrome (dark background, light text). qdbrowser's page area
(`about:blank`) is content, not chrome.

### Step 2 — open qnotebook's Settings dialog

```bash
qdwin_chord ctrl -- comma
sleep 2
qdwin_screenshot /tmp/pres01-step2-qnotebook-settings.png
```

**Assert (2.1):** OPEN the frame: a qnotebook Settings dialog with DARK
chrome is in front. If no dialog appeared, record 2.1 and 3.3 as ERROR (the
shortcut is all that is under test there) and continue.

### Step 3 — the shell switches to light; every running app follows

```bash
pres_qs_ipc darkMode setLight
SNAP_LIGHT=$(pres_wait_mode light 30) || echo "FAIL: shell did not publish light"
echo "SNAP_LIGHT=$SNAP_LIGHT"
sleep 3
PIDS_3=$(pres_app_pids); echo "PIDS_3=$PIDS_3"
qdwin_screenshot /tmp/pres01-step3-settings-light.png
# Reveal each app by closing the one above it.
qdwin_send_key KEY_ESC; sleep 1.5
qdwin_screenshot /tmp/pres01-step3-qnotebook.png
qdwin_vmx_merged "pkill -u admin -f '[q]notebook'"; sleep 2
qdwin_screenshot /tmp/pres01-step3-qdbrowser.png
qdwin_vmx_merged "pkill -u admin -f 'python3 -m [q]dbrowser'"; sleep 2
qdwin_screenshot /tmp/pres01-step3-qterminator.png
qdwin_vmx_merged "pkill -u admin -f '[q]terminator'"; sleep 2
qdwin_screenshot /tmp/pres01-step3-qfileman.png
```

**Assert (3.1):** `SNAP_LIGHT` mode is `light` and its generation differs
from `SNAP_DARK`'s.
**Assert (3.2):** `PIDS_3` equals `PIDS_1` exactly (no app relaunched before
the reveal).
**Assert (3.3):** OPEN `pres01-step3-settings-light.png`: the still-open
qnotebook Settings dialog now has LIGHT chrome.
**Assert (3.4):** OPEN the four reveal frames (qnotebook, qdbrowser,
qterminator, qfileman): the app in front of each has LIGHT chrome.

### Step 4 — kill qdshell; qfileman keeps running and keeps light

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

**Assert (4.1):** the `qfileman=` pid in `PIDS_4` equals the one in
`PIDS_1` (the other three were closed by the Step 3 reveal and read `none`).
**Assert (4.2):** `SNAP_4` equals `SNAP_LIGHT` (a shell crash does not
rewrite or delete the snapshot).
**Assert (4.3):** OPEN the Step 4 frame: qfileman is in front with LIGHT
chrome.

### Step 5 — a new app started while qdshell is DOWN reads the persisted snapshot

`systemctl stop` (unlike the Step 4 KILL) does not trigger the unit's
restart, so the shell stays down while the terminal starts. The snapshot
file's mtime is recorded before the stop and after the shell is started
again: if it is unchanged, the restarted shell did not rewrite it, so the
terminal's appearance can only have come from the persisted file.

```bash
MT_BEFORE=$(pres_admin "stat -c %Y.%i /var/lib/qdistro/presentation/current.json"); echo "MT_BEFORE=$MT_BEFORE"
pres_admin "systemctl --user stop qdshell.service"
pres_launch qterm2 qterminator
sleep 6
pres_admin "echo shell=\$(systemctl --user show -p ActiveState --value qdshell.service); pgrep -u admin -f -n '[q]terminator' >/dev/null && echo terminal=running || echo terminal=missing"
pres_admin "systemctl --user start qdshell.service"
sleep 8
MT_AFTER=$(pres_admin "stat -c %Y.%i /var/lib/qdistro/presentation/current.json"); echo "MT_AFTER=$MT_AFTER"
qdwin_screenshot /tmp/pres01-step5-new-terminal.png
```

**Assert (5.1):** the probe printed `shell=inactive` (ActiveState) and `terminal=running`
(the terminal started while the shell was down).
**Assert (5.2):** `MT_AFTER` equals `MT_BEFORE` (the snapshot was not
rewritten across the stop/start).
**Assert (5.3):** OPEN the Step 5 frame: the new terminal window is in front
and its chrome is LIGHT.

### Step 6 — the restarted shell switches back to dark; apps follow

```bash
PIDS_6A=$(pres_app_pids); echo "PIDS_6A=$PIDS_6A"
pres_qs_ipc darkMode setDark
SNAP_DARK2=$(pres_wait_mode dark 30) || echo "FAIL: restarted shell did not publish dark"
echo "SNAP_DARK2=$SNAP_DARK2"
sleep 3
PIDS_6=$(pres_app_pids); echo "PIDS_6=$PIDS_6"
qdwin_screenshot /tmp/pres01-step6-terminal-dark.png
qdwin_vmx_merged "pkill -u admin -f '[q]terminator'"; sleep 2
qdwin_screenshot /tmp/pres01-step6-qfileman-dark.png
```

**Assert (6.1):** `SNAP_DARK2` mode is `dark`; `PIDS_6` equals `PIDS_6A`.
**Assert (6.2):** OPEN both Step 6 frames: the terminal, then qfileman, are
in front with DARK chrome again.

## Cleanup

```bash
pres_kill_apps
pres_qs_ipc darkMode setDark >/dev/null
```

## Pass criteria

Every assert 1.1 → 6.2 passes. Machine asserts (1.1, 3.1, 3.2, 4.1, 4.2,
5.1, 5.2, 6.1) are decided by the printed values, not by pixels.

## Known-broken-if

- 3.2/4.1 FAIL (PIDs changed): an app restarted or crashed on a snapshot
  change or shell exit — see the app's `/tmp/pres-<tag>.log` in the guest.
- 3.4 FAIL for one app only: that app's `apply_presentation_update` path
  misses widgets; compare with the host four-app probe
  (`tests/integration/vm/presentation-four-apps.bats`).
- 4.2 FAIL: something deletes or rewrites `current.json` when qdshell dies.
- 5.1 FAIL (dark): a fresh process does not read the persisted managed
  snapshot (check `/usr/share/qdistro/presentation/deployment.json`).
