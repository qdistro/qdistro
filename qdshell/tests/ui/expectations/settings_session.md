# Settings → Session

What must be visible when this tab is open:

- A "Session behaviour" section header (i18n key
  `panels.session.section-behaviour`).
- A "Save session on logout" toggle (`panels.session.save-on-logout-label`).
- A "Restore session on login" toggle (`panels.session.restore-on-login-label`).
- A "Save current session" section header (`panels.session.section-save`) with
  a count of running applications that will be captured
  (`panels.session.current-apps-count`), a session-name text input
  (`panels.session.name-label`), and a "Save" button (disabled until a valid,
  non-duplicate name is entered).
- A "Saved sessions" section header (`panels.session.section-saved`). When no
  sessions are saved, the empty state (`panels.session.empty`) is shown;
  otherwise one row per saved session with its name, an application count
  (`panels.session.apps-count`), a "Restore" button, an overwrite icon button,
  and a delete icon button.
- The standard left-side Settings tab strip is visible.

Notes:
- qdwin exposes neither workspace mutation nor window-manager policy live-apply
  yet, so a persist-only banner (`panels.session.placement-persist-only`) is
  shown at the top: saving/restoring the *application set* works fully, but
  per-window placement / workspace assignment is saved only and not reapplied.
  The banner is gated on `SessionService.canApplyPlacement`
  (CapabilityService.workspaceMutation && wmPolicy — both currently false).
- Restoring launches each saved app via a safe argv array
  (SessionModel.buildLaunchArgv), never a shell string; appId/title strings from
  the live window list are treated as untrusted.
- The save name is validated against existing saved sessions (empty / too-long /
  duplicate names are rejected with an inline hint and a disabled Save button).
