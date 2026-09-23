# Bar (idle, no panel open)

What must be visible:

- A bar window pinned to one edge of the screen (default: top). Its position is whichever `Settings.data.bar.position` is set to.
- Bar widgets visible in their three sections (start / center / end), per the bar config. In a default config that typically includes:
  - A workspace indicator on the start.
  - A clock and/or media indicator near the center.
  - System tray, network, battery, control-center button on the end.
- The dock is visible if `Settings.data.dock.enabled` is true.
- No slide-out panel is currently overlaid on the bar.
- Background is the configured wallpaper, or empty/black if wallpaper management is disabled (likely in the test harness).
