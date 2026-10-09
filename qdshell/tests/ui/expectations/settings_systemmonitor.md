# Settings → System Monitor

What must be visible when this tab is open (the harness additionally clicks
"Thresholds" so both subtabs are judged):

- Tab header text: "System Monitor" (i18n key `system-monitor.title`).
- Subtab bar with: "General" and "Thresholds".
- On the General subtab: the discrete-GPU monitoring toggle, the custom
  highlight-colors toggle, and the external system monitor command field.
- On the Thresholds subtab: a per-metric threshold table (CPU usage, CPU
  temperature, memory, swap, disk, battery) with warning and critical
  columns of numeric stepper controls (NSpinBox — arrow adjusters, not
  sliders).
- The standard left-side Settings tab strip is visible.

Notes:
- Per-widget stat display toggles (showCpuUsage etc.) live in the bar
  widget's own settings (Bar/WidgetSettings/SystemMonitorSettings.qml),
  not on this tab — their absence here is correct, not a regression.
- Thresholds are numeric steppers by design; calling them "sliders" is
  stale upstream wording.
