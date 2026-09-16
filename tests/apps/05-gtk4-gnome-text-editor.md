# 05 — GTK4 native Wayland app: gnome-text-editor

<!-- qci:visual: required -->

**Acceptance criterion:** a GTK4 application running native-Wayland
(no XWayland) launches, accepts keystrokes that appear in the
document, maximises and restores cleanly. Smoke test for the GTK4
toolkit on qdwin.

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
qdwin_apps_launch gnome-text-editor "gnome-text-editor"
sleep 8
qdwin_apps_screenshot /tmp/05-step1-launched.png
```

**Assert (1.1):** screenshot shows the gnome-text-editor window with
"New Document" titlebar, an `Open` button at the top-left, hamburger
menu at the top-right, and an empty editing area.
**Assert (1.2):** bystander log shows
`toplevel_added handle=<N> ... xwayland=0` (it's native Wayland).

### Step 2 — type "qdwin"

```bash
qdwin_apps_type "qdwin"
sleep 1
GTK_STEP2_IMAGE=${QCI_GUI_ARTIFACT_DIR:-/tmp}/step2-typed.png
qdwin_apps_screenshot "$GTK_STEP2_IMAGE"
# Independent rendering evidence for the document canvas. This centered crop is
# wholly inside the editor at its launch geometry. A real black/absent GTK
# canvas has a near-zero grayscale mean; the normal white document is >0.9.
# Keep the threshold conservative so themes/antialiasing cannot manufacture a
# pass, while making a visual agent's black-background confusion impossible.
GTK_DOCUMENT_MEAN=$(magick "$GTK_STEP2_IMAGE" \
  -crop 320x240+480+280 +repage -colorspace Gray \
  -format '%[fx:mean]' info:)
printf 'gtk_document_center_mean=%s\n' "$GTK_DOCUMENT_MEAN" | \
  tee "${QCI_GUI_ARTIFACT_DIR:-/tmp}/step2-render-metric.txt"
awk -v mean="$GTK_DOCUMENT_MEAN" 'BEGIN { exit !(mean >= 0.70) }' || {
  echo "FAIL: GTK document center is black/absent (mean=$GTK_DOCUMENT_MEAN)"
  exit 1
}
```

**Assert (2.1):** screenshot shows `qdwin` rendered in the editing
area (cursor blinking after the `n`). The titlebar updates to show
`qdwin` (the auto-derived document title) with a small `Draft` label.
The black pixels surrounding the rounded white window are the expected bare
desktop background, not a black canvas. Report a rendering failure only when
the window's own document area or header is black/absent; visible `qdwin` in
the white document area plus the `qdwin`/`Draft` title satisfies this assert.
The required `gtk_document_center_mean >= 0.70` metric above independently
proves that the in-window document area is not black; when it passes, do not
reinterpret the surrounding desktop pixels as an absent document canvas.

### Step 3 — maximise

```bash
qdwin_apps_ctl "maxlast"
sleep 2
qdwin_apps_screenshot /tmp/05-step3-max.png
```

**Assert (3.1):** screenshot shows the editor filling the full
1280×800 output. Header bar is now the only chrome strip at the top.

### Step 4 — restore

```bash
qdwin_apps_ctl "restorelast"
sleep 2
qdwin_apps_screenshot /tmp/05-step4-restore.png
```

**Assert (4.1):** screenshot shows the editor back at approximately
its launch size (~700×550 px window, centred-ish, with black margin).
The typed text `qdwin` is still visible.

## Cleanup

```bash
qdwin_apps_ctl "close" || qdwin_apps_kill_all
```

## Pass criteria

- All four screenshots match assertions.
- "qdwin" appears in the document at step 2 (proves wl_keyboard
  delivery).
- Maximise/restore round-trip preserves the typed text.

## Known failure modes

- **Black canvas** — toplevel_added arrives but content stays black.
  GTK4 + qdwin pixman renderer would only fail this way if GTK4 went
  through a wl_egl_window path; the default GTK4 backend is shm so
  this should work. If it doesn't, file under
  .
- **dbus activation race** — gnome-text-editor exits immediately with
  "cannot open display" because GApplication registered via dbus
  without inheriting WAYLAND_DISPLAY. Track via
   item 2.
