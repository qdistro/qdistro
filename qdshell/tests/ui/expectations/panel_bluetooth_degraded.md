# Panel: Bluetooth — degraded (no adapter / Bluetooth off)

What must be visible when this panel is open on a host with NO Bluetooth
adapter or with Bluetooth disabled. The panel must degrade gracefully — it
opens and renders a coherent "unavailable / off" state, never a blank panel
or a crash:

- Header section with a bluetooth icon (the "off"/"bluetooth-off" variant is
  expected) and the title "Bluetooth".
- A close button (✕) in the top-right.
- A clear state message indicating Bluetooth is disabled or no adapter /
  no devices are available (e.g. an empty device list with an explanatory
  line). There must NOT be a populated list of connected devices.
- No error dialog, no blank/empty panel body, no partial render.
