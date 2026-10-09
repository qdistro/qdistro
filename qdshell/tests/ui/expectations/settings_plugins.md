# Settings → Plugins

What must be visible when this tab is open:

- Tab header text: "Plugins" (i18n key `panels.plugins.title`).
- Subtab bar with at least: "Installed", "Available", "Sources" (the
  harness clicks Available and Sources so all three subtabs are judged).
- An "Auto-update plugins" toggle and a "Check for updates" action.
- The Installed subtab's plugin list, populated by a fixture-seeded plugin:
  a row for "UI Fixture Plugin" showing its name and version, with a
  per-row enable/disable toggle and an uninstall (trash) button.
- Install actions on the Available subtab's plugin rows (if the registry is
  populated), and a repository / sources list on the Sources subtab.
- The standard left-side Settings tab strip is visible.
