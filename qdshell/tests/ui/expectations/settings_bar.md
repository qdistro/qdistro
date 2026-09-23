# Settings → Bar

What must be visible when this tab is open:

- Tab header text: "Bar" (i18n key `panels.bar.title`).
- Subtab bar with at least: "Appearance", "Widgets", "Behavior", "Monitors".
- An "Appearance" section heading.
- A "Position" dropdown (top / bottom / left / right).
- A "Density" dropdown.
- A "Type" dropdown.
- A "Display Mode" dropdown (always_visible / non_exclusive / auto_hide).
- The standard left-side Settings tab strip is visible.

## Launcher widget per-instance settings (Widgets subtab)

Opening the per-instance settings for a Launcher bar widget (via the Widgets
subtab editor, or right-click → "Widget settings" on the launcher in the bar)
shows the XFCE-style custom launcher-item editor:

- A "Select icon color" color choice (default launcher-button icon color).
- A "Show launcher panel button" toggle (i18n `bar.launcher.show-launcher-button-label`).
- A "Custom launcher items" section heading (i18n `bar.launcher.items-label`).
- An add-item form with three fields — "Name", "Icon" (with a "Browse" icon
  picker and a live icon preview), and "Command" — plus an "Add" button that is
  disabled until both Name and Command are non-empty.
- For each configured item: an inline editable Name field, an icon preview,
  reorder up/down arrows, and a remove (close) button, with the Icon and
  Command shown as editable fields on a second row.
- An empty-state message (i18n `bar.launcher.items-empty`) when no items exist.

Security note: item names/icons are untrusted labels and are NEVER part of the
executed command; clicking an item runs only its Command via
`execDetached(["sh","-lc",command])`.
