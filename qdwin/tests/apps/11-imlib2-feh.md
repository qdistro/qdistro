# 11 — Imlib2 / raw Xlib: feh

<!-- qci:visual: required -->

**Acceptance criterion:** an XWayland client that uses raw Xlib +
Imlib2 (no widget toolkit) renders an image correctly. feh is the
obvious test case — it has no menus, no keystroke handling beyond
arrow keys, just a window with a pixmap. Tests qdwin's basic Xlib
input/output round-trip without any toolkit-side smoothing.

## Setup

```bash
source ${QDWIN_REPO}/tests/apps/qdwin-apps-helpers.sh
qdwin_apps_set_vm "${VMNAME}"
qdwin_apps_session_up || { echo "FAIL: bystander/weston not healthy"; exit 1; }
qdwin_apps_kill_all
```

## Steps

### Step 1 — open a known PNG

The Firefox icon ships with the firefox package and is reliably
present at the path below. Pick a different image only if firefox is
intentionally not installed.

```bash
qdwin_apps_launch feh "feh /usr/share/icons/hicolor/256x256/apps/firefox.png"
sleep 4
qdwin_apps_screenshot /tmp/11-step1-firefox-icon.png
```

**Assert (1.1):** screenshot shows a window with the Firefox icon
(orange/yellow flame, blue globe) over a transparency checkerboard
background. Title bar reads `feh [1 of 1] - /usr/shar...` (truncated).
**Assert (1.2):** bystander log records the toplevel with
`xwayland=1`.

### Step 2 — verify size

```bash
qdwin_apps_ctl "maxlast"
sleep 2
qdwin_apps_screenshot /tmp/11-step2-max.png
```

**Assert (2.1):** the window now fills the screen and the image stays
its natural size (256×256 px, the same on-screen size as in step 1) —
not stretched or zoomed to the enlarged window. feh draws it CENTERED
in the enlarged window, over the checkerboard: on every resize feh
recomputes the image origin as `(window - image * zoom) / 2` with
zoom 1.0 unless `--scale-down`, `--zoom`/`--auto-zoom` or a
`--geometry` offset is given (feh `src/winwidget.c`,
`winwidget_render_image`), and this scenario passes none of those. A
centered natural-size image is therefore the correct result; the
failure signatures are an image scaled up to the window, an image
still drawn at the old (pre-maximise) window size/position with
unpainted or black area around it, or the window not filling the
screen (its frame not reaching the edges of the 1280×800 screen area —
see the 32 px band under Known failure modes). The test verifies
qdwin's configure-event handling (feh received the maximised size and
re-rendered), not feh's UX.

```bash
qdwin_apps_ctl "restorelast"
sleep 1
qdwin_apps_screenshot /tmp/11-step3-restore.png
```

**Assert (3.1):** window back at 256×256 + small chrome border. Image
fully visible.

## Cleanup

```bash
qdwin_apps_kill_all
```

## Pass criteria

- Image visibly rendered at step 1 (not all-black).
- Maximise expands the window, restore shrinks it.

## Known failure modes

- **`/usr/share/icons/hicolor/256x256/apps/firefox.png` missing** —
  use any other PNG; feh accepts any image format Imlib2 supports.
- **feh shows only the chrome with empty window content** —
  Imlib2's XComposite flow hit a qdwin path. File a regression and
  pin which qdwin commit broke it.
- **Maximised window leaves a ~32 px black band on every edge** (frame
  at roughly 32..1248 × 32..768 while the bystander reports
  `toplevel_geometry ... x=0 y=0 w=1280 h=800`) — open qdwin defect,
  first diagnosed 2026-10-01: weston's XWM frame keeps its invisible
  32 px shadow margin because libweston-desktop's XWayland surface has no
  `set_maximized` hook (the XWM never learns the shell maximised it),
  and qdwin places/sizes the XWayland wl_surface extent, margin
  included, at the work area. This is a FAIL of 2.1, not a pass with a
  note; cite it as this defect.
