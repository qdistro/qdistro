# Admin approval shortcut inventory — Qt app vs TUI

Source of the "one inventory" requirement:
`todo/security-hardening-carryforward.md` §"Admin audit UX":

> Admin Qt and TUI approval paths should share the same shortcut
> vocabulary where feasible; exact keybindings need one inventory.

This file is that inventory. The TUI's `BINDINGS`
(`tui/qdistro_admin_tui.py`) are the editable side; the Qt app's
`QShortcut` table (`admin_app/qdistro_admin_app.py`, ~lines 1695-1717,
help text ~2058-2084) is authoritative and **read-only** for this work.

## Side-by-side

| Action                                | Qt admin app            | TUI                              | Parity |
|---------------------------------------|-------------------------|----------------------------------|--------|
| Approve CURRENT request               | `Ctrl+Y`, `Alt+A`       | `Ctrl+Y`, `Alt+A`, `a`           | match (TUI adds single-key `a`) |
| Deny CURRENT request                  | `Ctrl+N`, `Alt+D`       | `Ctrl+N`, `Alt+D`, `d`           | match (TUI adds single-key `d`) |
| Create rule from CURRENT              | `Ctrl+R`                | `Ctrl+R`                         | match |
| Approve ALL pending (confirm)         | `Ctrl+Shift+A`          | `Ctrl+Shift+A`                   | match |
| Deny ALL pending (confirm)            | `Ctrl+Shift+D`          | `Ctrl+Shift+D`                   | match |
| Scope: once                           | `Ctrl+Shift+1`          | `Ctrl+Shift+1`, `1`              | match (TUI adds bare `1`) |
| Scope: 1 hour                         | `Ctrl+Shift+2`          | `Ctrl+Shift+2`, `2`              | match |
| Scope: 24 hours                       | `Ctrl+Shift+3`          | `Ctrl+Shift+3`, `3`              | match |
| Scope: forever (any command)          | `Ctrl+Shift+4`          | `Ctrl+Shift+4`, `4`              | match |
| Scope: forever (this exact program)   | `Ctrl+Shift+5`          | `Ctrl+Shift+5`, `5`              | match |
| Scope: forever (this exact argv)      | `Ctrl+Shift+6`          | `Ctrl+Shift+6`, `6`              | match |
| Scope: forever (argv basename)        | `Ctrl+Shift+7`          | `Ctrl+Shift+7`, `7`              | match |
| Scope: forever (argv prefix)          | `Ctrl+Shift+8`          | `Ctrl+Shift+8`, `8`              | match |
| Approve all in SELECTED silo (confirm)| `Alt+Shift+A`           | — (none)                         | Qt-only, justified |
| Deny all in SELECTED silo (confirm)   | `Alt+Shift+D`           | — (none)                         | Qt-only, justified |
| Navigate queue                        | list-widget arrows      | `Up` / `Down`                    | equivalent |
| Refresh queue                         | (auto / timer)          | `r`                              | TUI-only convenience |
| Help                                  | Help menu action        | `?`                              | TUI-only convenience |
| Quit                                  | window close            | `q`                              | TUI-only convenience |

## Divergences and their justification

1. **`Alt+Shift+A` / `Alt+Shift+D` (per-silo bulk decide) — Qt only.**
   The Qt app has a selectable silo in its pending view, so "approve all
   in the *selected* silo" is meaningful. The TUI has a single flat
   pending queue with no silo selection widget, so there is no
   "selected silo" to scope a bulk decide to. Adding the binding without
   the underlying selection concept would be a no-op or, worse, a
   misleading footgun. **Intentionally not added.**

2. **Single-key `a` / `d` and bare `1`..`8` — TUI only.**
   These are *additive* TUI ergonomics over a TTY (no modifier gymnastics
   on a serial console / drop-shell). They do not replace or shadow the
   shared `Ctrl+*` vocabulary — both fire the same actions. The bare
   single-key approve/deny is acceptable here because the TUI's bulk
   path (the dangerous one) still requires `Ctrl+Shift+*` + an explicit
   confirmation modal, mirroring the Qt blast-radius guard.

Everything in the shared approval/scope vocabulary matches exactly.

## Keep this in sync

If either surface changes an approval/scope binding, update the matching
side **and** this table in the same change. The TUI binding table lives
in `AdminTuiApp.BINDINGS`; the Qt table lives in the `_mk_shortcut(...)`
block of `admin_app/qdistro_admin_app.py`.
