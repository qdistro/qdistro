# Settings → Appearance

The harness opens this tab via `settings openTab appearance`, which resolves to
`SettingsPanel.Tab.Appearance` in IPCService. What must be visible (top to
bottom, single scrollable column):

- A "Workspaces" section header (`panels.appearance.workspaces-header`) with:
  - A workspace-count spin box (`workspace-count-label`, range 1–32).
  - One text input per workspace for its name (`workspace-name-label` + index).

- A "Themes" section header (`panels.appearance.themes-header`) with:
  - An icon-theme combo (`icon-theme-label`), discovered from
    `/usr/share/icons/`; first entry is "System default" (`system-default`).
  - A GTK-theme combo (`gtk-theme-label`), discovered from `~/.themes`,
    `~/.local/share/themes`, and `/usr/share/themes` (dirs containing a
    `gtk-3.0/` or `gtk-4.0/` subdir); first entry is "System default".

- A cursor block (no extra header) with:
  - A cursor-theme combo (`cursor-theme-label`), discovered from
    `/usr/share/icons/`.
  - A cursor-size spin box (`cursor-size-label`, range 16–64, step 8).

- A "Font rendering" section header (`panels.appearance.font-rendering-header`)
  with:
  - A font-DPI spin box (`font-dpi-label`, range 0–400; 0 = system default).
  - An antialiasing toggle (`font-antialias-label`).
  - A hinting toggle (`font-hinting-label`).
  - A hinting-style combo (`font-hintstyle-label`): None / Slight / Medium /
    Full.
  - A subpixel-order combo (`font-rgba-label`): None / RGB / BGR / VRGB / VBGR.

- A "Toolbar & menus" section header (`panels.appearance.icon-policy-header`)
  with:
  - A "Show icons in menus" toggle (`icons-in-menus-label`).
  - A "Show icons in buttons" toggle (`icons-in-buttons-label`).

- A sound-theme block (no extra header) with a sound-theme combo
  (`sound-theme-label`), discovered from `/usr/share/sounds` and
  `~/.local/share/sounds` (dirs containing an `index.theme`).

- The standard left-side Settings tab strip is visible.

Notes:
- Theme/font names are sanitized via the pure `GtkSettings.isSafeName` before
  ever reaching a shell string; unsafe names (shell-meta, path traversal) are
  filtered out of the discovery results and never selectable.
- All controls persist to the user's GTK `settings.ini` (gtk-3.0 + gtk-4.0);
  font rendering additionally writes `~/.config/fontconfig/fonts.conf` for
  non-GTK apps. These are file writes consumed by GTK/fontconfig, not qdwin
  compositor commands, so no capability gate is needed.
- Selecting "System default" on any theme combo removes the corresponding
  managed key (reverts to the system default).
