# Settings → Default Applications

The harness opens this tab via `settings openTab defaultapps`
(`SettingsPanel.Tab.DefaultApps`).

Top of the tab (always visible):
- A description paragraph (`panels.default-apps.description`) explaining the
  settings are written to the XDG `mimeapps.list`.
- A sub-tab strip (`NTabBar`) with two tabs:
  - "Categories" (`panels.default-apps.subtab-categories`)
  - "File Types" (`panels.default-apps.subtab-mime-editor`)

State-dependent (mutually exclusive on `DefaultAppsService.ready`):
- A "Scanning installed applications…" placeholder (`panels.default-apps.scanning`)
  while the service is not yet ready.
- A "Rescan applications" button (`panels.default-apps.rescan`) below the
  sub-tab view (only once ready), applying to both sub-tabs.

## Categories sub-tab (default)

The pre-existing per-category choosers, one block each for:
browser, mail, fileManager, terminal, textEditor, imageViewer, audioPlayer,
videoPlayer (`panels.default-apps.category-<id>` + `-description`). Each block:
- A combo of installed apps that declare support, first entry "System default"
  (`panels.default-apps.system-default`).
- A "Currently using: {app}" hint (`panels.default-apps.currently-using`) when
  "System default" is selected but a system handler is resolved.
- A "Reset to system default" button (`panels.default-apps.reset-to-default`)
  when an explicit override is set.

## File Types sub-tab (the MIME-type-level association editor)

- A description (`panels.default-apps.mime-editor-description`).
- A search box (`panels.default-apps.mime-search-placeholder`) that filters the
  catalog by MIME string OR friendly description (e.g. "image/png" or "PDF").
- A result-count line (`panels.default-apps.mime-result-count`, `{count}`).
- A bounded-height scrollable list of MIME types. Each row shows:
  - The MIME type string and (when available) its friendly description from
    `/usr/share/mime/<type>.xml`.
  - A handler combo of installed apps that declare support for that type, first
    entry "System default".
  - A "Currently using: {app}" hint when no explicit override is set.
  - A "No installed application declares support…" hint
    (`panels.default-apps.mime-no-handlers`) when the type has no handlers.
  - A "Reset to system default" button when an explicit override is set.
- A "No file types match your search." line
  (`panels.default-apps.mime-no-results`) when the filter is empty.

Notes:
- The MIME catalog is built from installed `.desktop` files' `MimeType=` entries
  (plus any type already present in system/user `mimeapps.list`), aggregated and
  validated by the pure `Services/System/MimeAssociations.js` (shared with the
  Node tests).
- Setting a default runs `xdg-mime default <app.desktop> <mime/type>` as a fully
  tokenized argv via `Quickshell.execDetached` (NO shell). Clearing rewrites only
  the `[Default Applications]` section of `~/.config/mimeapps.list`, leaving
  `[Added Associations]`/`[Removed Associations]` untouched.
- MIME type strings and `.desktop` ids are UNTRUSTED: both are validated by
  `MimeAssociations.isValidMimeType` / `isValidDesktopId` before any command is
  built. A value containing shell metacharacters, whitespace, a slash (for ids)
  or `..` is rejected and never reaches the command line. These are XDG file/CLI
  operations, not qdwin compositor commands, so no capability gate is needed.
