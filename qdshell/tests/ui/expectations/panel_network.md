# Panel: Network

What must be visible when this panel is open:

- Header section showing a network icon (wifi / ethernet / disconnected variant) and the title (i18n `common.wifi` or `common.ethernet`).
- A close button (✕) in the top-right of the header.
- A mode switcher / tabs to flip between WiFi and Ethernet views.
- WiFi view: enable toggle and EITHER a list of nearby SSIDs with
  signal-strength icons and connection status badges OR — on hardware with
  no Wi-Fi radio, like this VM — an explicit "no Wi-Fi networks found" /
  unavailable state. A fabricated or stale SSID list is a defect.
- Ethernet view: per-interface details (IP, gateway, DNS) when an interface is up.
- Inline error banners can appear with their own small close (✕) button — this is expected, not a regression.
