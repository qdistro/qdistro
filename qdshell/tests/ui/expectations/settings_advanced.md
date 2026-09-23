# Settings → Advanced

What must be visible when this tab is open:

- A prominent warning header at the top, styled in the error/danger color,
  with a warning title ("Advanced raw settings editor",
  `panels.advanced.warning-title`) and a description explaining that this
  editor writes raw settings values directly and can break the shell
  (`panels.advanced.warning-description`). A warning/alert triangle icon
  sits to its left.
- A search box (`panels.advanced.search-label`) with a magnifier icon and a
  "Filter by key path…" placeholder (`panels.advanced.search-placeholder`).
  Typing filters the list below by dotted key path (case-insensitive
  substring), e.g. "power.lidCloseOnAC".
- A result-count line below the search box
  (`panels.advanced.result-count`, "Showing N of M settings").
- A scrollable tree/list of setting rows, one per leaf in the settings tree.
  Each row shows:
  - the dotted key path (in a monospace font),
  - a "changed from default" marker dot when the current value differs from
    its default, plus a "Default: …" hint line
    (`panels.indicator.default-value`),
  - a type-aware editor on the right: a toggle for booleans, a text/number
    input for numbers and strings, and a read-only/disabled JSON field for
    arrays and objects,
  - a per-row reset button (rotate icon, `panels.advanced.reset-tooltip`)
    that is enabled only when the value differs from its default.
- When the search matches nothing, a centered empty-state message
  (`panels.advanced.no-results`) is shown instead of rows.
- The standard left-side Settings tab strip is visible.

Notes:
- Editing complex values (arrays/objects) is out of scope: those rows are
  displayed as read-only JSON and their editor is disabled.
- Displayed values track Settings.data live: edits made elsewhere in the
  shell are reflected here (via a periodic refresh and the settings-saved
  signal).
