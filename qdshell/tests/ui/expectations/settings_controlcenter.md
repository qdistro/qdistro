# Settings → Control Center

What must be visible when this tab is open (default subtab is "Appearance"):

- Tab header text: "Control Center" (i18n key `panels.control-center.title`).
- Subtab bar showing at least: "Appearance", "Cards", "Shortcuts".
- On the active (Appearance) subtab: a "Position" dropdown for where the control-center panel appears (e.g. "Close to bar button"), and any related layout settings such as a "System monitor disk path" selector.
- The standard left-side Settings tab strip is visible.

Notes:
- The card list (Profile / Shortcuts / Audio / Brightness / Weather / Media toggles) and the reorder UI live on the "Cards" subtab and are NOT expected to be visible by default. The test only verifies the subtab is reachable.
