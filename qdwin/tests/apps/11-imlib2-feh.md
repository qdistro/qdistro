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

**Assert (2.1):** the image stays its natural size (256×256 px) but
the window now fills the screen — feh draws the image in the upper-
left of an enlarged window, not stretched. Behaviour is feh-specific
("don't auto-zoom on maximise"); the test verifies qdwin's
configure-event handling, not feh's UX.

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
