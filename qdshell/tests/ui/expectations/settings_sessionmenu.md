# Settings → Session Menu

What must be visible when this tab is open (default subtab is "General"):

- Tab header text: "Session Menu" (i18n key `session-menu.title`).
- Subtab bar showing at least: "General" and "Actions".
- On the active (General) subtab: settings that shape the session-menu UI itself — typical entries include a "Large buttons style" toggle, a layout dropdown, "Show keybinds" toggle, a "Enable countdown timer" toggle, and a "Countdown duration" slider.
- The standard left-side Settings tab strip is visible.

Notes:
- The per-action toggles (Lock, Suspend, Hibernate, Reboot, Logout, Shutdown) live on the "Actions" subtab and are NOT expected to be visible by default. The test only verifies the subtab is reachable.
