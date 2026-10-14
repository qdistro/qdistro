# Panel: Tray Drawer

What must be visible when this panel is open:

- In a production environment with tray items, this panel renders a compact grid of unpinned system-tray icons. Each cell shows one icon with a hover tooltip.
- The panel has no standard header bar / close button — it's a transient drawer dismissed by tap-outside.
- **In the headless UI-test environment there are no DBus tray items**, and with no items the drawer deliberately does not stay on screen: TrayDrawerPanel auto-closes when `trayValues` is empty, including when it is opened already empty (`onOpened` → `root.close()`, TrayDrawerPanel.qml). PASS condition: the toggle IPC succeeds (no IPC error), the screenshot is captured, and the frame shows only the normal bar and the desktop/wallpaper below it — no visible drawer surface, no empty or half-rendered drawer stub, no stale or placeholder tray icons, no other panel/surface, no crash dialog. A frame with nothing but the bar and an unobstructed desktop is the right answer here; do NOT require a visible empty drawer.

Notes:
- To exercise the populated rendering, seed a tray item via DBus before the capture (out of scope for this harness). Because the empty drawer auto-closes, this empty-state capture cannot see whether the drawer opened at all; it detects a drawer that fails to auto-close, stale/placeholder icons, or a broken shell, not a drawer that never opens.
- The tray **known-items policy** UI (per-item show/hide + "Reset" button) is NOT part of this drawer surface. It lives in the Tray bar widget's settings dialog (TraySettings.qml), opened via right-click → widget settings on the Tray widget. That transient dialog has no dedicated UI-test manifest surface, so it is not screenshot-asserted here; its logic is covered by the Node test `tests/test_tray_known_items.js`.
