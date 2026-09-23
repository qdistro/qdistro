# Panel: Battery

What must be visible when this panel is open:

- Header section showing a battery icon (state depends on charge level) and the title "Battery" (i18n `common.battery`).
- A close button (✕) in the top-right of the header.
- A charge-level card: one row per detected battery (laptop + any Bluetooth peripherals).
  Each row shows: device label, percentage (e.g. "78%"), and a progress bar.
- A time-remaining text for the primary battery.
- If `showPowerProfiles` is enabled: a "Power Profile" section with a 3-step slider (powersaver / balanced / performance) and three icons.
- If `showQdshellPerformance` is enabled: a Qdshell Performance toggle.
- If no batteries are detected, the charge card is hidden and only the header is visible.
