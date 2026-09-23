# Panel: Tray — degraded (no StatusNotifier tray items)

What must be visible when this panel/menu is opened with no system-tray
(StatusNotifierItem) applications registered. The panel must degrade
gracefully:

- The tray surface opens.
- It shows an empty / "no tray items" state rather than stale or placeholder
  icons.
- No blank surface and no crash.
