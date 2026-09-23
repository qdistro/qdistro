# Settings → Accessibility

The harness opens this tab via `settings openTab accessibility`, which lands on
the default ("Find Cursor") sub-tab. What must be visible:

- A sub-tab strip across the top with four tabs: "Find Cursor", "Keyboard",
  "Mouse", and "Assistive" (i18n keys `panels.accessibility.tab-find-cursor`,
  `tab-keyboard`, `tab-mouse`, `tab-assistive`), with "Find Cursor" selected.
- The Find Cursor sub-tab content (the default, so this is what renders):
  - A "Find cursor" section header (`panels.accessibility.find-cursor-section`).
  - An enable toggle (`find-cursor-enable-label`).
  - A read-only shortcut/IPC-command field showing `qs ipc call findCursor show`
    (`find-cursor-shortcut-label`) plus a note field for the user's chosen
    shortcut binding (`find-cursor-shortcut-note-label`).
  - A "Test" / preview button (`find-cursor-test-button`) with its explanatory
    label (`find-cursor-test-label`).
  - A ring color picker (`find-cursor-ring-color-label`).
  - A ring-size spin box in px (`find-cursor-ring-size-label`).
  - A duration spin box in ms (`find-cursor-duration-label`).
- The standard left-side Settings tab strip is visible.

Notes:
- Only the default ("Find Cursor") sub-tab content is rendered after
  `openTab accessibility`; the harness does not click sub-tabs. The other three
  sub-tabs (reached by selecting their strip entry) cover:
  - Keyboard: a "Keyboard" accessibility section
    (`panels.accessibility.keyboard-section`) with Sticky Keys, Slow Keys (+ a
    delay spin box in ms), and Bounce Keys (+ a delay spin box in ms) toggles
    (`sticky-keys-label`, `slow-keys-label`, `bounce-keys-label`).
  - Mouse keys: a "Mouse keys" section (`panels.accessibility.mouse-keys-section`)
    with a mouse-keys enable toggle (`mouse-keys-label`) and a pointer speed
    spin box (`mouse-keys-speed-label`).
  - Assistive: an "Assistive technologies" section
    (`panels.accessibility.assistive-section`) with an AT-SPI / assistive-tech
    autostart toggle (`assistive-autostart-label`).
- The Keyboard / Mouse-keys / Assistive controls are capability-gated: when no
  xkb accessx (or AT-SPI) backend is reachable the controls are still shown but
  disabled, with a red unavailable note (`keyboard-backend-unavailable` /
  `assistive-backend-unavailable`) and the toggle descriptions fall back to
  `backend-unsupported-note`. Settings are persisted regardless.
- The find-cursor trigger is a compositor-level global shortcut qdshell cannot
  register itself; the shortcut field is read-only and only documents the IPC
  command to bind.
