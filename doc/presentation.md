# Application presentation snapshot

First-party Qt Widgets apps follow qdshell appearance through one validated
JSON snapshot. qdshell remains the settings owner. Consumers import
`qdistro_presentation` (distribution name `qdistro-presentation`) and never
read qdshell's private `settings.json` / `colors.json` or its Quickshell
singletons.

This is the v1 contract. The library lives at `sdk/presentation/`.

## File

UTF-8 JSON, at most 64 KiB, no external references or code. Installed
location on a qdistro desktop:

```
/var/lib/qdistro/presentation/current.json
```

The directory is created by the installer, owned by the trusted admin
account, mode 0755; the file is 0644. Isolated applications receive
read-only access. Tier-2 binds that directory with private propagation
and does not mount sibling `/var/lib/qdistro` trees. There is no default
`current.json` at install time: absence means native fallback, not a
forced dark theme.

On an ordinary (non-qdistro) desktop, qdshell may publish to
`${XDG_STATE_HOME:-$HOME/.local/state}/qdistro/presentation/current.json`.
`QDISTRO_PRESENTATION_FILE` selects a fixture path for ordinary apps; it is
ignored by the locker and polkit prompt.

## Schema

`version` is 1. `mode` is `dark` or `light` and describes this target
palette. `generation` is a SHA-256 of the canonical content, formatted as a
UUID; it changes only when normalized content changes. `enabled` is a
required boolean: `false` resets consumers to their no-snapshot fallback
while keeping every other required field. Deletion or unreadability retains
last-known-good state.

Example using qdshell's built-in dark target palette:

```json
{
  "version": 1,
  "enabled": true,
  "generation": "3925a2c7-c2f2-5304-64c2-bee9dac764ee",
  "mode": "dark",
  "colors": {
    "mPrimary": "#fff59b", "mOnPrimary": "#0e0e43",
    "mSecondary": "#a9aefe", "mOnSecondary": "#0e0e43",
    "mTertiary": "#9bfece", "mOnTertiary": "#0e0e43",
    "mError": "#fd4663", "mOnError": "#0e0e43",
    "mSurface": "#070722", "mOnSurface": "#f3edf7",
    "mSurfaceVariant": "#11112d", "mOnSurfaceVariant": "#7c80b4",
    "mOutline": "#21215f", "mShadow": "#070722",
    "mHover": "#9bfece", "mOnHover": "#0e0e43"
  },
  "fonts": {
    "uiFamily": "Sans Serif", "fixedFamily": "monospace",
    "basePointSize": 11, "uiScale": 1.0, "fixedScale": 1.0
  },
  "metrics": {"uiScale": 1.0, "radiusRatio": 1.0, "inputRadiusRatio": 1.0},
  "motion": {"disabled": false, "speed": 1.0},
  "tooltipsEnabled": true,
  "iconTheme": ""
}
```

The sixteen color fields are required opaque `#rrggbb` values. Text pairs
(`mSurface`/`mOnSurface`, primary, secondary, tertiary, error, hover) must
meet a 4.5:1 contrast ratio; `mSurfaceVariant`/`mOnSurfaceVariant` must meet
3:1. A failing palette is rejected as a whole.

`tooltipsEnabled` is exported from qdshell `ui.tooltipsEnabled`. Motion
exports configured reduced motion (`general.animationDisabled` /
`general.animationSpeed`), not the shell's transient power-profile override.
`iconTheme` empty means restore the captured native icon-theme name.

Unknown versions are rejected. Unknown fields inside v1 are ignored. New
optional fields may extend v1 with defined defaults; a required behavioral
change needs v2.

## Consumer behaviour

Apps attach one `PresentationController` per `QApplication` after
construction and before the first window. Theme mode `system` follows a
valid snapshot ("Follow desktop") and is the install default. `dark` and
`light` keep each app's explicit legacy palette. `native` restores the
captured platform style and palette.

Local overrides live in each app's own config under `appearance` (version 1).
Missing keys inherit. An empty `icon_theme` override means the captured
native icon theme. UI point size is `basePointSize * fonts.uiScale *
metrics.uiScale`; monospace UI size uses `fixedScale`. Content monospace
(opt-in terminal/notebook) uses `basePointSize * fonts.fixedScale` and does
not follow UI scale.

A missing, unreadable, or untrusted file retains last-known-good state. A
repaired file recovers without restart. Only `enabled: false` clears the
inherited shared layer.

## Publisher

`qdistro-presentation-publish` reads bounded stdin JSON and atomically
replaces `current.json` (exclusive temp, fsync, `os.replace`, directory
fsync). It skips the write when the content hash matches the existing file.
Publication failure is a diagnostic, never fatal to qdshell.
