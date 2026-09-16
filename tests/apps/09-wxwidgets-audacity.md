# 09 — wxWidgets via XWayland: audacity

<!-- qci:visual: required -->

**Acceptance criterion:** a wxWidgets application (audacity) launches
via XWayland, renders its main window with menus/toolbars/track view,
shows the first-run welcome popup. wxWidgets is one of the rarer
toolkits we want regression coverage for — Wayland-native wx is not
production-ready in 2026 so XWayland is the only path.

## Setup

```bash
source ${QDWIN_REPO}/tests/apps/qdwin-apps-helpers.sh
qdwin_apps_set_vm "${VMNAME}"
qdwin_apps_session_up || { echo "FAIL: bystander/weston not healthy"; exit 1; }
qdwin_apps_kill_all
```

## Steps

### Step 1 — launch

```bash
# Force the X11/GDK backend so wxGTK runs through XWayland (the launch helper
# exports GDK_BACKEND=wayland by default; this scenario asserts xwayland=1, so
# wxWidgets must go via XWayland, not native Wayland).
qdwin_apps_launch audacity "env GDK_BACKEND=x11 audacity"
sleep 12
qdwin_apps_screenshot /tmp/09-step1-launched.png
```

**Assert (1.1):** screenshot shows the audacity main window:
titlebar "Audacity", menu bar (File / Edit / Select / View /
Transport / Tracks / Generate / Effect / Analyze / Tools / Help),
playback transport buttons (pause / play / stop), and a "Welcome to
Audacity!" first-run popup with "Try the new Audacity 4 Beta release",
the "Test the Beta release" action, and the "View tutorials" / "Visit our
forum" links.
**Assert (1.2):** bystander log records `xwayland=1` for the audacity
toplevel.

### Step 2 — dismiss welcome popup

```bash
qdwin_apps_send_key KEY_ESC
sleep 1
qdwin_apps_screenshot /tmp/09-step2-after-esc.png
```

**Assert (2.1):** the welcome popup is gone; the main audacity
window is fully visible. Tests xdg-popup dismissal via Escape from
an XWayland-spawned modal.

### Step 3 — open File menu via Alt+F

```bash
qdwin_apps_chord alt -- f
sleep 1
qdwin_apps_screenshot /tmp/09-step3-menu.png
```

**Assert (3.1):** screenshot shows the File menu open with at least
"New" / "Open…" / "Recent Files" / "Quit" entries.

```bash
qdwin_apps_send_key KEY_ESC
```

## Cleanup

```bash
qdwin_apps_kill_all
```

## Pass criteria

- audacity main window + welcome popup visible at step 1.
- Escape closes the popup.
- Alt+F opens File menu.

## Known failure modes

- **Welcome popup captures keyboard for several seconds** — the
  wx-side modal grabs input synchronously; if step 2's Escape fires
  too early, send another. The scenario gives a 12s wait for full
  initialization.
- **Audacity can't find audio device** — the welcome popup now reads
  "Audacity could not find any audio devices". Still passes step 1
  (window rendered). Note for the audio-stack follow-up only.
