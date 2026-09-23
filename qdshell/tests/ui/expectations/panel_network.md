# Panel: Network

What must be visible when this panel is open:

- Header section showing a network icon (wifi / ethernet / disconnected variant) and the title (i18n `common.wifi` or `common.ethernet`).
- A close button (✕) in the top-right of the header.
- A mode switcher / tabs to flip between WiFi and Ethernet views.
- WiFi view: enable toggle, a list of nearby SSIDs with signal-strength icons and connection status badges.
- Ethernet view: per-interface details (IP, gateway, DNS) when an interface is up.
- Inline error banners can appear with their own small close (✕) button — this is expected, not a regression.
