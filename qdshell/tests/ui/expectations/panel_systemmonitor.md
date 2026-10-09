# Panel: System Monitor / Stats

What must be visible when this panel is open:

- Header section showing a "device-analytics" icon and the title "System Monitor" (i18n `system-monitor.title`).
- A close button (✕) in the top-right of the header.
- A CPU card: usage percentage, current frequency (GHz), and CPU temperature (°C), with a live sparkline/graph.
- A Memory card: usage percentage and the used amount in GB (the product
  renders `NN% (X.X GB)` — used only, no total — by design; see
  SystemStatsPanel.qml), with a live usage graph/sparkline.
- A Network card: RX and TX rates with units, with a graph.
- A detailed-stats card showing load average (1/5/15 minute), GPU
  temperature (if available), disk usage %, and — only when swap is
  configured on the host — swap usage (the swap rows are `visible:
  swapTotalGb > 0`; a VM with no swap correctly shows none).
