# Bar (idle, no panel open)

What must be visible (the shipped default layout, `Assets/settings-default.json`
`bar.widgets`):

- A bar window pinned to the top edge of the screen.
- Start (left) section: a launcher button icon, a clock showing the time and
  date as text, and a few small icon-only indicators after it.
- Center section: a workspace indicator — a row of small pills/circles, the
  active one highlighted and showing its number.
- End (right) section: a group of at least four small icon-only buttons — fewer
  than four is a failure. The first is a notification bell and the second a
  speaker/volume icon; both are required. (The remaining two are the brightness
  and control-center glyphs; only their exact identity is not checked.)
- No slide-out panel, launcher, or menu is currently overlaid on the bar or the
  screen below it.
- Background is the configured wallpaper, or empty/plain dark if wallpaper
  management is disabled (likely in the test harness).

Not required (these legitimately render nothing in the test VM, so their
absence is not a failure): the system tray (no tray items), the battery (no
battery), the active-window title and media indicator (no focused window or
player), the taskbar (no open windows), and the dock (default `auto_hide`, so
it is hidden at rest).
