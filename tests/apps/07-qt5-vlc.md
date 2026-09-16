# 07 — Qt5 widget app: VLC media player

<!-- qci:visual: required -->

**Acceptance criterion:** Qt5 (QWidget) UI renders correctly through
qdwin's XWayland path and survives `max`/`restore`. VLC is the
designated Qt5 reference because its main window uses QWidget /
QMainWindow rather than the QQuick (OpenGL) path that the pixman
renderer can't composite.

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
qdwin_apps_launch vlc "vlc --intf qt --no-qt-error-dialogs"
sleep 10
qdwin_apps_screenshot /tmp/07-step1-launched.png
```

**Assert (1.1):** screenshot shows the VLC main window: titlebar
"VLC media player", menu bar (Media / Playback / Audio / Video /
Subtitle / Tools / View / Help), the iconic VLC traffic-cone logo
centred in the content area, and the playback transport bar at the
bottom.
**Assert (1.2):** bystander log records the toplevel with
`xwayland=1`.

### Step 2 — maximise

```bash
qdwin_apps_ctl "maxlast"
sleep 2
qdwin_apps_screenshot /tmp/07-step2-max.png
```

**Assert (2.1):** VLC fills the screen; cone is now centred over a
larger black canvas.

### Step 3 — restore

```bash
qdwin_apps_ctl "restorelast"
sleep 2
qdwin_apps_screenshot /tmp/07-step3-restore.png
```

**Assert (3.1):** VLC returns to its launch size with the cone
centred. (Pre-fix bug #1 would leave it at the maximised dimensions —
this scenario doubles as a Qt5-side regression check for that fix.)

## Cleanup

```bash
qdwin_apps_ctl "close" || qdwin_apps_kill_all
```

## Pass criteria

- VLC visibly rendered (no all-black canvas).
- max + restore cycle returns approximately to launch size.

## Known failure modes

- **All-black canvas** — VLC's main video output uses an
  XComposite-via-XWayland path that should work even under pixman.
  If it's black, file as a regression rather than a renderer-choice
  issue and bisect against the qdwin commit.
- **Native Qt5 wayland mode crash** — running VLC with
  `QT_QPA_PLATFORM=wayland` (default in our launcher env) and no
  `--intf qt` override may hit the wl_egl_window path and stay black.
  The scenario passes `--intf qt` explicitly to avoid it. Removing
  the flag is the test for the renderer-pixman-vs-gl follow-up.
