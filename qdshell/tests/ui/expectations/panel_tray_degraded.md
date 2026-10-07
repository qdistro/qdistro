# Panel: Tray — degraded (no StatusNotifier tray items)

What must be visible when this panel/menu is opened with no system-tray
(StatusNotifierItem) applications registered. The panel must degrade
gracefully:

- TrayDrawerPanel auto-closes when `trayValues` is empty — with no
  StatusNotifierItem apps registered the drawer deliberately renders
  nothing. The graceful-degradation contract is therefore: the toggle IPC
  succeeds, the shell stays alive, and the frame shows only the normal
  desktop/bar — no stale or placeholder tray icons, no half-rendered
  drawer, no crash dialog.
- (The non-degraded tray golden documents that a populated drawer needs an
  SNI fixture; the empty case is exactly this auto-close path.)
