# Panel: Tray Drawer

What must be visible when this panel is open:

- In a production environment with tray items, this panel renders a compact grid of unpinned system-tray icons. Each cell shows one icon with a hover tooltip.
- The panel has no standard header bar / close button — it's a transient drawer dismissed by tap-outside.
- **In the headless UI-test environment there are no DBus tray items**, so the panel is *correctly* empty. PASS condition: the panel opens (no IPC error), the screenshot is captured, and no other panel/surface is present in the frame other than the bar and wallpaper. An empty drawer is the right answer here.

Notes:
- To exercise the populated rendering, seed a tray item via DBus before the capture (out of scope for this harness). The PanelShell refactor doesn't touch tray rendering, so the empty-state check is enough to detect a regression that breaks the panel from opening at all.
- The tray **known-items policy** UI (per-item show/hide + "Reset" button) is NOT part of this drawer surface. It lives in the Tray bar widget's settings dialog (TraySettings.qml), opened via right-click → widget settings on the Tray widget. That transient dialog has no dedicated UI-test manifest surface, so it is not screenshot-asserted here; its logic is covered by the Node test `tests/test_tray_known_items.js`.
