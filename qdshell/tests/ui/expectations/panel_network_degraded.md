# Panel: Network — degraded (offline / no connection)

What must be visible when this panel is open while the host is offline
(no active WiFi/Ethernet connection). The panel must degrade gracefully:

- Header section with a network icon (the disconnected variant is expected)
  and a title.
- A close button (✕) in the top-right.
- The WiFi/Ethernet content area renders a coherent "not connected" /
  "no networks" / scanning state — an empty SSID list or a disconnected
  message is expected, NOT a populated connected-network view.
- An inline error/info banner may appear; that is expected, not a regression.
- No blank panel body and no crash.
