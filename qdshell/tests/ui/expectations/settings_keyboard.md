# Settings → Keyboard

The harness opens this tab via `settings openTab keyboard`, which lands on the
default ("General") sub-tab. What must be visible:

- A sub-tab strip across the top with three tabs: "General" (`common.general`),
  "Layout" (`panels.keyboard.layout-title`), and "Shortcuts"
  (`panels.keyboard.shortcuts-title`), with "General" selected.
- The General sub-tab content (the default, so this is what renders):
  - A "Use system defaults" toggle (`panels.keyboard.use-system-defaults-label`)
    governing the layout block.
  - A "Typing" section (`panels.keyboard.section-typing`) with a key-repeat
    delay spin box in ms (`repeat-delay-label`) and a repeat-rate spin box in Hz
    (`repeat-rate-label`).
  - A test-area text input (`panels.keyboard.test-area-label`) for trying out
    the repeat settings.
  - A "Cursor" section (`panels.keyboard.section-cursor`) with a persist note
    (`cursor-persist-note`), a cursor-blink toggle (`cursor-blink-label`), and a
    cursor-blink-rate spin box in ms (`cursor-blink-rate-label`).
  - A NumLock restore toggle (`panels.keyboard.restore-numlock-label`).
- The standard left-side Settings tab strip is visible.

Notes:
- Only the default ("General") sub-tab content is rendered after
  `openTab keyboard`; the harness does not click sub-tabs. The "Layout" sub-tab
  (reached by selecting its strip entry) covers:
  - A "Keyboard model" section (`panels.keyboard.section-model`) with a model
    combo (`model-label`).
  - A "Layouts" section (`panels.keyboard.section-layouts`) with its description
    (`layouts-description`), one row per configured layout (layout name +
    variant combo + move-up / move-down / remove buttons), and an "Add layout"
    searchable combo (`add-layout-label`).
  - An "Options" section (`panels.keyboard.section-options`) with a layout
    switch-shortcut combo (`switch-shortcut-label`), a Compose-key combo
    (`compose-key-label`), and an "XKB options" group (`xkb-options-label`) of
    checkboxes (caps:swapescape, caps:escape, caps:none, terminate, altwin:menu).
- The "Shortcuts" sub-tab (reached by selecting its strip entry) covers:
  - A persist-only capability banner (`panels.keyboard.shortcuts-capability-note`)
    explaining that the qdwin compositor owns global hotkeys and does not yet bind
    them, so these settings are saved but not applied. This always shows (qdwin
    has no register-shortcut request yet), mirroring the Mouse-tab gating.
  - A "Navigation keybinds" section (`panels.general.keybinds-title`) listing the
    shell-owned navigation actions (up/down/left/right/enter/escape/remove), each
    rendered as an NKeybindRecorder with editable/removable key-combo slots, plus
    a "Reset to defaults" button (`panels.keyboard.shortcuts-reset-all`) that
    restores every navigation keybind to its built-in default. Each binding can
    be re-recorded (edit), cleared (remove), or added.
  - A "Custom application shortcuts" section
    (`panels.keyboard.custom-shortcuts-title`) with a description
    (`custom-shortcuts-description`) noting commands run without a shell. When the
    list is empty an empty-state line shows (`custom-shortcuts-empty`); otherwise
    one card per custom shortcut shows its name, combo + command (monospace), and
    edit / remove buttons. A card whose combo collides with another shortcut is
    outlined in the error color and shows a conflict warning
    (`custom-shortcuts-conflict`).
  - An add/edit editor card with a Name input (`custom-shortcuts-name-label`), a
    Command input (`custom-shortcuts-command-label`), a key-combination recorder
    (`custom-shortcuts-combo-label`, single combo), and an
    "Add shortcut" / "Save" button (`custom-shortcuts-add-button` / `common.save`);
    a "Cancel" button appears while editing an existing entry. Committing an
    incomplete entry (missing combo or command) surfaces a warning toast
    (`custom-shortcuts-incomplete-title`); committing a command that re-enters a
    shell (e.g. `sh -c ...`) is refused with a warning
    (`custom-shortcuts-shell-title`); committing a combo that collides with
    another shortcut warns but still saves (and the card is flagged).
  - Conflict detection spans BOTH the navigation keybinds and the custom
    shortcuts: two entries that normalise to the same canonical combo (modifier
    order / case independent, per Services/Keyboard/ShortcutConflicts.js) are
    flagged.
- A capability note banner (`panels.keyboard.capability-note`) appears at the
  top of the General sub-tab when no live-apply backend exists
  (KeyboardInputService.persistOnly); settings are persisted only in that case.
- Key repeat and cursor blink are behavior settings and are NOT scoped by
  "Use system defaults"; that toggle (per XFCE) only governs the model / layout
  / options block, whose controls disable when it is on.
