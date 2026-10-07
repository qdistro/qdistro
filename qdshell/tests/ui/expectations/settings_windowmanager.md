# Settings → Window Manager

What must be visible when this tab is open:

- A "Focus" section header (i18n key `panels.window-manager.section-focus`).
- A focus-policy combo (`panels.window-manager.focus-policy-label`) offering
  "Click to focus" and "Focus follows mouse".
- A focus-follows-mouse delay spin box (in ms), enabled only when the focus
  policy is "Focus follows mouse".
- "Raise window on click" and "Raise window on hover" toggles.
- A "Placement" section with a new-window placement combo
  (`panels.window-manager.placement-label`) offering Center / Under the mouse /
  Smart / Cascade.
- A "Snapping & Tiling" section with a snapping/edge-tiling toggle
  (`panels.window-manager.snap-enabled-label`) and a snap-distance spin box (px),
  the spin box enabled only when snapping is on.
- A "Titlebar & Decorations" section with a titlebar double-click action combo
  (Maximize / Shade / Minimize / Do nothing) and a free-text decoration-theme
  name input (`panels.window-manager.decoration-theme-label`).
- A "Keyboard Shortcuts" section with a note
  (`panels.window-manager.shortcuts-note`) and free-text accelerator inputs for:
  close window, toggle maximize, toggle fullscreen, tile left, tile right.
- The standard left-side Settings tab strip is visible.

Notes:
- When the compositor cannot apply window-manager policy
  (`WindowManagerService.canApplyWmPolicy` false — only on qdwin shell
  protocol < v35), a persist-only banner
  (`panels.window-manager.backend-persist-only`) is shown at the top of the
  tab and controls save without applying. On qdwin >= v35 the capability is
  advertised and the banner is correctly ABSENT — the judge must not
  require it. There is never probing for or dispatch to sway / labwc / any
  other compositor.
- The decoration-theme name and shortcut strings are treated as untrusted free
  text. Shortcut accelerators are validated against a strict allowlist, so a
  malicious accelerator can never be persisted as a usable shortcut.
