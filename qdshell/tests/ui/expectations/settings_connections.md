# Settings → Connections

What must be visible when this tab is open:

- Tab header text: "Connections" (i18n key `panels.connections.title`).
- Subtab bar with at least: "WiFi" and "Bluetooth".
- A WiFi networks section: enable toggle and (if any) network list with signal indicators.
- A master Bluetooth control: a "Bluetooth" toggle (off and disabled when no
  Bluetooth adapter is present — `bluetoothAvailable` gates it), using a
  bluetooth-off icon when disabled.
- Device and pairing lists ("Connected", "Paired", "Available") render only
  when a Bluetooth adapter exists and is enabled
  (`visible: ... && BluetoothService.enabled` in BluetoothSubTab.qml). On a
  VM with no Bluetooth hardware these sections legitimately do not render —
  the Bluetooth subtab then shows the master toggle alone.
- The standard left-side Settings tab strip is visible.

Notes:
- The harness additionally clicks the "Bluetooth" subtab after opening so the
  judged description covers both subtabs.
