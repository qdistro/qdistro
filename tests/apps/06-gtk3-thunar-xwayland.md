# 06 — GTK3 + XWayland: thunar file manager

<!-- qci:visual: required -->

**Acceptance criterion:** a GTK3 file manager (thunar) launches via
XWayland, displays a populated file grid, and survives `max`/`restore`
cycles. Validates the GTK3-via-XWayland path which most legacy desktop
apps depend on.

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
qdwin_apps_launch thunar "env GDK_BACKEND=x11 thunar"
sleep 8
qdwin_apps_screenshot /tmp/06-step1-launched.png
```

**Assert (1.1):** screenshot shows thunar with menu bar
(File / Edit / View / Go / Bookmarks / Help), navigation bar with
Back/Forward/Up/Home buttons, a Places sidebar (Computer / admin /
Recent / Trash / File System / Browse Network), and a file grid with
several folder icons.
**Assert (1.2):** bystander log line for thunar must show
`xwayland=1`. Going through the GTK3 native-Wayland path silently
falling back to X11 is the symptom we want to catch.
**Assert (1.3):** weston pid did not change since Setup
(no SIGSEGV-style restart).

### Step 2 — maximise

```bash
qdwin_apps_ctl "maxlast"
sleep 2
qdwin_apps_screenshot /tmp/06-step2-max.png
```

**Assert (2.1):** thunar fills the screen, file grid expands to show
more icons.

### Step 3 — restore

```bash
qdwin_apps_ctl "restorelast"
sleep 2
qdwin_apps_screenshot /tmp/06-step3-restore.png
```

**Assert (3.1):** thunar back at ~870×510-ish launch size; file grid
visible.

## Cleanup

```bash
qdwin_apps_ctl "close" || qdwin_apps_kill_all
```

## Pass criteria

- All three screenshots populated (no all-black).
- bystander log tags thunar as `xwayland=1`.
- weston pid unchanged.

## Known failure modes

- **dbus-activated env loss** — first launch goes through dbus
  activation that drops `WAYLAND_DISPLAY`/`DISPLAY`; thunar never
  appears. Fix per  item 2
  (`dbus-update-activation-environment`).
- **No XWayland** — `Xwayland` binary missing from baseweed-baked.
  Same item 1 in the followups doc.
