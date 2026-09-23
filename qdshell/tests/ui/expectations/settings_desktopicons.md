# Settings → Desktop Icons

The harness opens this tab via `settings openTab desktopicons`
(`SettingsPanel.Tab.DesktopIcons`). What must be visible:

- The standard left-side Settings tab strip is visible.
- An "Enable desktop icons" toggle (`desktop-icons.enabled-label`) with a
  description. This is OFF by default.
- A block of dependent controls (rendered but disabled/greyed when desktop icons
  are off — they are still present in the layout):
  - A single-click vs double-click activation toggle
    (`desktop-icons.single-click-label`).
  - A "Show hidden files" toggle (`desktop-icons.show-hidden-label`).
  - A sort-mode chooser (`desktop-icons.sort-label`) offering at least "Name"
    and "Type" (`desktop-icons.sort-name`, `sort-type`).
  - An "arrange folders first" toggle (`desktop-icons.folders-first-label`).
  - An icon-size spin box in pixels (`desktop-icons.icon-size-label`).
  - A label-size spin box in points (`desktop-icons.label-size-label`).

Notes:
- The dependent controls are bound to the enable toggle (`enabled:` on the inner
  column), so when desktop icons are off they appear dimmed/disabled but are
  still visible in the screenshot.
