# UI guidelines

## Aesthetic target

**XFCE + Terminator.** Utilitarian, dense, keyboard-first, standard Qt
widgets end-to-end, no per-app reskinning. This is a deliberate choice —
reject the "beautiful first, usable later" framing common in modern DEs.
Consistency beats novelty.

## Core principles

### Everything is right-clickable — and there are no toolbars

Every interactive surface has a context menu. App windows, list rows, tree
nodes, tabs, status-bar elements, notification items, launcher results,
panel areas, empty desktop regions, selected text, selected files —
*everything*.

**qdistro apps have no toolbars.** Actions live in three places: the menu
bar (dropdowns, for app-wide actions like File / Edit / View), context
menus (for actions on specific items), and keyboard shortcuts. That's it.
No toolbar, no ribbon, no icon strip.

This is a deliberate constraint. Toolbars bloat the top of every window,
duplicate menu actions, and crowd out content. Menu bar + context menus +
keyboard shortcuts cover every action, at zero vertical-space cost.
Frequently-used actions get prominent shortcuts; less-used ones live in
menus.

Non-interactive empty regions also have context menus: empty panel area →
"Add widget"; empty desktop → "Change wallpaper" (admin-only).

### Standard Qt widgets throughout

`QPushButton`, `QMenu`, `QListView`, `QTreeView`, `QTableView`,
`QLineEdit`, `QComboBox` — Qt as it ships, with the system theme. No
custom button widgets. No per-app reskinned menus. No "signature" look
that hides platform conventions.

Every first-party app feels like the same family. Third-party Qt apps
drop in without looking alien.

### Keyboard-first

Every action has a shortcut. Menus have mnemonics (underlined letters).
Tab navigation always works. Every app follows the standard shortcut
table (below). Point-and-click is always available but never the only
path.

### Consistency over novelty

Same words mean the same actions. Same colours mean the same things. Same
affordances work the same way across apps. Surprise is cost.

## Context-menu architecture

### Universal item model

Every "thing" a user can right-click on has a **type identifier + payload**:

- `file` — path + mime
- `url` — URL + mime hint
- `text` — string + source context
- `image` — path or inline blob
- `contact`, `tab`, `terminal-selection`, `notebook-cell`, `vault-item`,
 ... — open-ended

Apps declare item types for their selectable content.

### Menu assembly order

Context menu is composed from six sources, in this order:

1. **App-specific actions** — the owning app's actions for this item
 ("Open," "Rename," "Delete" in a file manager).
2. **Built-in system actions** — "Copy," "Copy path," "Properties," always
 present for types that support them.
3. **"Send to..." submenu** — dynamically populated based on item type +
 available destinations. The biggest single thing the DE contributes.
4. **Plugin-contributed actions** — installed plugins that declare
 handling for this item type.
5. **Recall actions** — "Find where this appeared before" (opens the
 recall viewer filtered by this item; only if recall-user is
 configured).
6. **"Report for debugging"** — always present at the very bottom; packs
 item + context into a diagnostic tarball.

Each section is visually separated.

### "Send to..." — the universal verb

Modeled on Android's share intent. Any item can be sent somewhere.

- Destinations are apps and services that declared they accept the item's
 type.
- Destinations span **all user silos** — e.g., right-click a URL in
 work-user's browser → "Send to..." → "dev-user's terminal (as curl),"
 "admin's read-later," "dev-user's notebook (as code cell)."
- Each destination is visually badged with the **owning user's colour**.
- Cross-user transfers go through `qbus-admin` — admin policy can
 pre-approve, prompt, or deny.
- Destinations outside allowed policy are hidden, not shown-and-disabled —
 reduces clutter.

### SDK hooks (first-party apps)

`qdistro_app` exposes:

```python
@qdistro_app.context_menu(item_type='url')
def add_url_actions(item):
 return [MenuItem(label="Open in incognito", callback=open_incognito)]

@qdistro_app.receives(item_type='url', label="Open as bookmark")
def handle_received_url(item):
 add_bookmark(item.url)
```

The first decorator registers the app's own actions for matching types;
the second registers the app as a destination for "send to..." for the
given type. Apps declare what they expose and what they consume; the DE
wires the rest.

### Plugin system

Plugins are files under `/etc/qdistro/context-menu-plugins/` (global) or
`~/.config/qdistro/context-menu-plugins/` (per-user). TOML manifest:

```toml
[plugin]
name = "url-to-work-browser"
item_types = ["url"]
section = "send_to"
label = "Open in work-user's Firefox"
command = "qsu -u work-user firefox {url}"
icon = "firefox"

[policy]
requires_approval = false
```

`command` can reference item fields (`{url}`, `{path}`). Privileged
actions go through [qsu](sudo.md), so they inherit admin approval.

### Discoverability

Right-click is powerful but invisible. Conventions to mitigate:

- Tooltip on hover over any right-clickable surface includes "right-click
 for more."
- A ⋮ (kebab) button appears in unobtrusive spots (list-row on hover,
 titlebar corner, tab-bar overflow) that opens the *same* context menu.
 Every right-click action is reachable by left-click — without a toolbar.
- First-time help overlay on install calls out right-click explicitly.

## Visual design

### Colour

- **Dark theme is the default.** Light theme exists and is tested as a
 secondary option.
- System-wide dark/light toggle; per-user override allowed.
- **Per-user accent colour** (assigned at account creation) drives:
 - Window border / titlebar tint for windows owned by that user.
 - Selection highlight inside that user's apps.
 - Chips / badges in cross-user surfaces (launcher, notifications,
 context menu).
- Admin's accent is a neutral slate or graphite.

### Typography

- Sans default: **Inter** (primary UI font), with **Noto Sans** as fallback
 for non-Latin scripts via Qt's font substitution chain.
- Monospace: **JetBrains Mono** (or per-user preference).
- Fixed scale ladder: 10 / 12 / 14 / 16 / 20 / 28 pt. No arbitrary sizes.

### Density

- Default: **compact** (XFCE-ish). Narrow list rows, tight menus, small
 padding.
- **Comfortable** mode available (larger padding, wider rows) for touch
 or low-vision users.

### Iconography

- One icon set — **Papirus** or similar complete library.
- Used across all first-party apps.
- Emoji allowed in user *content* but not in UI chrome.

## Windows and panels

### Panel

- Single horizontal panel, **bottom by default** (XFCE muscle memory; user
 can relocate).
- Contains: app menu, task list, system tray, workspace indicator, clock,
 sensitive-device indicator, recall-active indicator.
- Items configurable but defaults cover 95% of users without fiddling.
- Right-click panel region → "Add widget," "Panel properties," etc.

### Window decorations

- Standard Qt client-side or server-side per system theme.
- Close / minimize / maximize on the right (consistent, no swap).
- **User-colour border** on every window.
- Handed-off windows keep the source-user colour.

#### Silo badge

Toplevels bridged from a separate-uid silo (tier-3) are visually
distinguished with a **silo-coloured chip** rendered at the very left of
the titlebar, plus a silo-coloured border (overriding the per-uid colour,
since the waypipe-client half always runs as admin uid). The chip text is
the silo username. The chip background is a darker shade of the titlebar
colour, with white text — visible on both dark and light themes.

### Launcher (app menu)

- Keyboard-driven search + categorized browse.
- **Ctrl+Space** opens.
- Search covers app names, command names, recent files, recall results
 (in recall-user session), browser history, open tabs.
- Results tagged with source-user colour when cross-user.

## Dialogs

Follow **Delphi / Windows 95-era conventions.** These had decades of
usability validation, are keyboard-complete, and match the muscle memory
of users who grew up on XFCE / classic Windows. Don't invent.

### Layout

- Titlebar: meaningful text (not just the app name or "Dialog").
- Body: question or controls, single column if simple; grouped in
 `QGroupBox` when complex.
- Button bar at the bottom, right-aligned, fixed height.
- Max width ~500 px by default; grow vertically rather than horizontally.
- **Dimensions don't change mid-interaction.** Dialogs that grow or shrink
 while the user reads them are disorienting.

### Button order (left to right)

```
Primary affirmative → Secondary affirmative → Cancel → Help
```

Examples:

- Yes/No/Cancel: `[Yes] [No] [Cancel] [Help]`
- Save prompt: `[Save] [Don't Save] [Cancel] [Help]`
- Properties: `[OK] [Cancel] [Apply] [Help]`

Explicitly **Windows / Delphi order, not macOS Cancel-last.** qdistro's
roots are XFCE / classic-Linux; Windows-style ordering is the shared
muscle memory.

### Default button

The primary affirmative is the default, visually emphasized via Qt's
default-button highlight.

### Keyboard

- **Enter** → activates the default button (unless focus is on a control
 that consumes Enter, e.g., multi-line text edit).
- **Escape** → activates Cancel.
- **F1** → opens Help (Help button is also always present; both paths
 work).
- **Alt+letter** → activates a button by its underlined mnemonic. Every
 button has one.
- **Tab / Shift+Tab** → move focus through controls top-to-bottom,
 left-to-right.

### Modality

**Modal by default.** Modeless dialogs are the exception; when used, they
appear in the task list with full window decoration. Modality is a
feature — it signals "this needs your answer before anything else" —
not a limitation to avoid.

### Alert dialogs

Use `QMessageBox` with the appropriate icon type:

| Type | Icon | Buttons |
|----------|------------|------------------------------------------------------------------------|
| Info | blue "i" | `[OK]` |
| Warning | yellow "!" | `[Continue] [Cancel]` (default: Cancel — non-destructive) |
| Error | red "x" | `[OK]`, optionally `[Retry] [Details]` |
| Question | blue "?" | `[Yes] [No] [Cancel]` (Cancel present when there's a dismiss state) |

Long messages: `[Show details >]` expander keeps the dialog compact by
default.

### Reuse the standard Qt dialog family

Use `QDialog`, `QMessageBox`, `QInputDialog`, `QFileDialog`,
`QColorDialog`, `QFontDialog` directly. Subclass `QDialog` for custom
layouts but preserve the conventions above. Never invent parallel dialog
frameworks per app.

## Machine-readable UI

Every dialog and context menu must be programmatically introspectable.
Two purposes:

1. **Accessibility** — screen readers, `dogtail`, AT-SPI tooling.
2. **AI agents** — autonomous GUI task execution. qdistro is designed so
 an agent can "know what's on screen" and "click this button" reliably.

Two layered surfaces, both mandatory:

### AT-SPI

Qt emits AT-SPI natively. Tooling (Orca screen reader, `dogtail`,
`at-spi2-tools`) can walk the widget tree and enumerate roles, labels,
states. Conventions:

- Every widget has a stable `objectName` unique within its parent.
- Buttons have `.accessibleName` distinct from the label when label
 clarity alone isn't enough.
- Dynamic content emits AT-SPI change events.

### qdistro UIModel D-Bus interface

`com.qdistro.App1.UIModel` (extends the SDK interface):

```
GetDialogTree() -> json # if a dialog is currently open
GetContextMenuTree() -> json # if a context menu is currently showing
GetWindowTree() -> json # the full top-level window structure

ActivateWidget(object_path: str) # click / activate by ID
SetWidgetValue(object_path: str, value: variant)
```

Each node carries: `id`, `role`, `label`, `enabled`, `shortcuts`,
`item_type`, `value`, `children`, and qdistro-specific metadata (item
types, send-to destinations with user ownership, approval tokens required
for sensitive actions).

AT-SPI and UIModel present the same logical structure. UIModel adds
qdistro context that AT-SPI doesn't express.

### Test requirement

Every first-party app has an `accessibility_coverage` test that opens each
registered dialog offscreen and asserts both AT-SPI and UIModel return
introspectable trees. Fails CI if any dialog breaks introspection.

## Report for debugging

Every context menu includes a **"Report for debugging"** action at the
bottom. One click produces a tarball containing everything needed to
analyze an issue with the right-clicked item.

### Contents

`qdistro-report-<timestamp>-<uuid>.tar.gz` containing:

| File | Contents |
|---------------------|------------------------------------------------------------------------------|
| `manifest.json` | Timestamp, app, qdistro version, user, item type, item payload (sanitized). |
| `ui-tree.json` | UIModel + AT-SPI tree of the current window / dialog at report time. |
| `screenshot.png` | Window screenshot; password fields automatically redacted. |
| `app-state.json` | App-specific state the SDK contributes. |
| `logs.txt` | Last ~500 lines from app log + journalctl for the app's process. |
| `recent-events.json`| Recent UI events (focus changes, button clicks) if enabled. |
| `env.json` | OS version, kernel, Qt version, DPI, display info. |
| `sanitizer.log` | Record of what was redacted and why. |

### SDK hook

```python
@qdistro_app.report_contributor
def contribute_to_report(ctx):
 ctx.add_file("my-state.json", json.dumps(self.state))
 ctx.redact_path("/home/work-user/secrets.txt")
 ctx.add_log_tail("/var/log/my-app.log", 200)
```

### Sanitization (automatic)

- Password fields masked in screenshots and UI trees.
- Clipboard content excluded.
- URLs / paths matching `/etc/qdistro/recall-exclusions.yaml` redacted.
- The user can preview the report + manually redact fields before
 delivery.

### Delivery

- **Default**: save to `~/Downloads/qdistro-reports/<filename>.tar.gz`.
- **Share with admin**: copies to `/var/lib/qdistro/reports/` for admin
 triage.
- **Upload to bug tracker**: admin-configured endpoint (GitHub issue,
 Sentry, etc.); requires user confirmation per upload.

## Accessibility

- Qt accessibility bridge (AT-SPI) always on.
- Font-scale ramp via standard Qt scaling; tested at 100%, 125%, 150%,
 200%.
- High-contrast theme available.
- Screen reader (`orca`) tested with every first-party app.
- Keyboard-only usage path through every feature.

## Standard keyboard shortcuts

Enforced in every first-party app and encouraged for third-party:

| Action | Shortcut |
|-----------------------------------|-------------------------------------|
| Copy / Cut / Paste | Ctrl+C / X / V |
| Undo / Redo | Ctrl+Z / Ctrl+Shift+Z |
| Select all | Ctrl+A |
| Find / Find next / Find prev | Ctrl+F / F3 / Shift+F3 |
| Save | Ctrl+S |
| Close tab / close window | Ctrl+W / Ctrl+Shift+W |
| New tab | Ctrl+T |
| Quit app | Ctrl+Q |
| Rename selected | F2 |
| Delete selected | Del |
| Contextual help | F1 |
| Switch app | Alt+Tab |
| Switch workspace | Ctrl+Alt+← / → |
| Launcher | Ctrl+Space |
| Right-click menu via keyboard | Menu key or Shift+F10 |
| "Send to..." menu on selection | Ctrl+Shift+S |

## Drag-and-drop = implicit "send to..."

Dragging any item onto another app is equivalent to the "Send to..." menu
action with that app as destination:

- Same type filtering.
- Same cross-user policy gating.
- Same destination visual cues on drop targets.

## Reference: KDE HIG with qdistro deltas

Where qdistro doesn't have a specific reason to deviate, follow the
**KDE Human Interface Guidelines**. qdistro is Qt-based; KDE is the
largest Qt-based desktop with a thought-through HIG; closer alignment
means less cognitive friction.

qdistro-specific deviations from KDE:

- **No toolbars.** KDE often uses toolbars; qdistro doesn't.
- **XFCE-like compact density** over KDE's default roomier spacing.
- **Terminator idioms** in the terminal specifically.
- **Right-click everywhere** as a first-class principle.
- **Machine-readable UI** contract (UIModel D-Bus interface).
- **Source-user colour tinting** across cross-user surfaces (qdistro-
 specific because of the multi-silo model).
- **Dialog button order matches Win95 / Delphi**, not necessarily KDE's.
