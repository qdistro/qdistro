# Settings → Display

What must be visible when this tab is open (default subtab is "Layout"; the
harness additionally clicks "Brightness" so both subtabs are judged):

- Tab header text: "Display" (i18n key `panels.display.title`).
- Subtab bar showing at least: "Layout", "Brightness" and "Night Light".
- On the Layout subtab: a monitor-arrangement section with one card per
  detected monitor (enable toggle, resolution dropdown, scale, rotation).
- On the Brightness subtab: a brightness slider section, with one row per
  detected monitor when multiple displays are attached.
- The standard left-side Settings tab strip is visible.

Notes:
- Night-Light controls (toggle, start/end time) live on the "Night Light"
  subtab and are NOT expected to be verified here. The test only requires
  the subtab to exist in the bar.
