# Settings → Connections

What must be visible when this tab is open:

- Tab header text: "Connections" (i18n key `panels.connections.title`).
- Subtab bar with at least: "WiFi" and "Bluetooth".
- A WiFi networks section: enable toggle and (if any) network list with signal indicators.
  The Wi-Fi subtab is the default view and is captured BEFORE the harness
  clicks Bluetooth; it also shows an "Airplane Mode" toggle above the Wi-Fi
  toggle (`WifiSubTab.qml`).
- A master Bluetooth control: a "Bluetooth" toggle (off and disabled when no
  Bluetooth adapter is present — `bluetoothAvailable` gates it), using a
  bluetooth-off icon when disabled. "Off" means the switch thumb sits at the
  LEFT end of its track (the shared NToggle convention); the dimming is the
  separate disabled state. The full-frame views must show this Bluetooth
  toggle row, its label and its dimmed/disabled state; the thumb POSITION of
  this one switch is established by the zoomed crop below, not by the
  full-frame views.
- The zoomed crop of the Bluetooth master switch (the "=== zoomed crop" view,
  taken from the Bluetooth subtab frame) shows a switch track outline with the
  round knob at the LEFT end of that track. That crop is magnified and
  contrast-stretched specifically so the knob position is observable; it is
  the evidence for the switch's position (the full-frame view at 1280x800
  cannot resolve the dim off-state outline reliably). Its colors are
  stretched, so judge dimming/disabled state from the full-frame view, not
  from the crop. The crop view also carries a "Harness pixel measurement"
  line, computed from the unstretched frame; it must say the knob is at the
  LEFT end (the harness already fails the test outright otherwise).
  The crop is AUTHORITATIVE for the Bluetooth master switch's thumb
  position: the full-frame describer cannot resolve this low-contrast
  disabled switch, so a full-frame view that places the Bluetooth thumb at
  the RIGHT end (or calls it on) is not evidence about its position, either
  way, and must NOT be reported as MISSING, contradicted or EXTRA when the
  crop and the measurement both show the LEFT end. This exemption covers
  only the Bluetooth master switch's thumb position; every other element
  (rows, labels, dimming, the other switches) is still judged from the
  full-frame views. A crop showing the knob at the RIGHT end, or no switch,
  fails this bullet; so does a crop showing the knob in the middle, or a
  measurement line that does not say LEFT.
- Device and pairing lists ("Connected", "Paired", "Available") render only
  when a Bluetooth adapter exists and is enabled
  (`visible: ... && BluetoothService.enabled` in BluetoothSubTab.qml). On a
  VM with no Bluetooth hardware these sections legitimately do not render —
  the Bluetooth subtab then shows the master toggle alone.
- The standard left-side Settings tab strip is visible.

Notes:
- The harness additionally clicks the "Bluetooth" subtab after opening so the
  judged description covers both subtabs.
- It then crops the Bluetooth master switch out of that Bluetooth frame,
  magnifies it 8x and stretches its contrast (`ZoomCrop` in manifests.py),
  and describes that crop as one more view. It also measures the knob's
  end from the unstretched pixels (`runner.measure_switch_knob`) and fails
  the capture unless it is the LEFT end, whatever any description says.
