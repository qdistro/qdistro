# Window hierarchy (windows with sub-units)

## Goal

The window manager's window list has a two-level hierarchy: each top-level
window has zero or more **sub-units** (browser tabs, terminal panes, notebook
tabs, IDE editor tabs). Sub-units are first-class nodes in the WM. The user
can:

- See all tabs / panes / cells across all windows at once.
- Switch focus directly to a sub-unit (click a tab in the WM list → the
 browser focuses that tab).
- Close a sub-unit from the WM.
- Search across sub-unit titles and content (feeds into [recall](recall.md)).

Standard Wayland compositors see only surfaces. This feature requires apps
to cooperate by reporting their internal sub-units.

## SDK extension — subunit provider

`qdistro_app` exposes a subunit interface.

```python
@dataclass
class Subunit:
 id: str # stable within the window
 kind: str # "tab" | "pane" | "cell" | custom
 title: str
 active: bool # currently focused inside the app
 metadata: dict # e.g., {"url": "https://..."} for tabs
```

App-provided callbacks:

```python
@qdistro_app.subunit_provider
def list_subunits() -> list[Subunit]:
 ...

qdistro_app.subunits_changed.emit() # call when list changes
```

WM-invoked actions:

```python
@qdistro_app.subunit_activate
def activate(id: str):
 ... # bring this tab / pane / cell to focus

@qdistro_app.subunit_close
def close(id: str):
 ...
```

D-Bus interface: `org.qdistro.App1.Subunits` on the app's session bus.

## WM consumption

The compositor listens on `org.qdistro.App1.Subunits` for each client window
whose app registers as a provider. For those windows the WM shows a
collapsible sub-list:

```
Firefox (work-user) [blue border]
├── qdistro design spec — GitHub
├── gmail inbox
└── hacker news

Terminal (dev-user) [green border]
├── ~/project (bash) [active]
└── logs (tail -f)

Notebook (work-user) [blue border]
├── Notebook A
└── Notebook B [active]
```

The parent window's user-colour tint propagates to its sub-units.

## Browsers — extension and bridge feed sub-units

Browsers don't import `qdistro_app`. Instead, the existing bridge plumbing
(see [browser](browser.md)) provides subunit data:

1. The extension tracks tabs per browser-window.
2. The bridge forwards tab-list updates.
3. `qdistro-browser` **impersonates** `org.qdistro.App1.Subunits` on behalf
 of each browser top-level — it registers a virtual subunit provider keyed
 to each browser window's Wayland surface.
4. The WM treats it identically to a first-party provider.

This unifies browser tabs with other apps' sub-units under one WM
abstraction.

### Browser-window ↔ Wayland-surface mapping

Challenge: the extension reports tabs keyed by the browser's internal
window-id. Need to correlate with Wayland top-level surfaces.

- **Clean path**: the browser runs inside a container with a nested
 compositor (see [window-handoff](window-handoff.md)). The nested
 compositor sees browser surfaces directly; extension window-ids are
 matched via title / pid / timing.
- **Direct path**: the browser runs as a native Wayland client of admin's
 compositor. Correlation by title / pid is heuristic — good enough in
 practice but edge-case-prone.

Prefer nested-in-container for browsers.

### Activation flow

The WM wants to activate tab T in browser B's subunit list:

1. The WM calls `org.qdistro.App1.Subunits.Activate("tab:T")` on B's
 virtual provider.
2. The virtual provider (`qdistro-browser`) converts to a bridge call:
 `tabs.activate(T)`.
3. The bridge → the extension → `browser.tabs.update(T, {active: true})`.
4. The browser focuses the tab.

## Terminal

The PyQt terminal manages its tabs / panes internally.

- Each tab and pane gets a stable ID.
- Title = current command + cwd (e.g., `bash: ~/project`).
- Metadata: shell type, cwd, pid of foreground process.
- Activate → switch to that tab / pane.
- Close → close the pane (prompt if a foreground process is running).

## Notebook

Two levels are possible (notebook tabs, individual cells), but:

- **Notebook tabs are subunits; cells are not.** The WM list becomes
 unwieldy with 100 cells per notebook.
- **Cell-level content** still flows via the text-snapshot path (feeds
 recall) without being a WM navigation unit.

## Third-party multi-window apps (IDEs, browsers' own internals)

Don't implement `qdistro_app`. Two paths:

- **Browsers** — use the bridge route above.
- **IDEs and others** — run them in their container+nested-compositor. The
 nested compositor treats the IDE's top-level windows as subunits of the
 container's outer surface. Coarser than editor-tabs but gives structure.

## Operations on subunits

- **Activate** (focus).
- **Close**.
- **Rename** (optional; not all subunit kinds support it).
- **Drag out** — tabs especially: pop a tab into its own window.
- **Drag across windows** — merge a tab into another browser window.
- **Pin / unpin**.

All go through D-Bus methods on the app's subunit interface; the WM provides
UI affordances.

## Subunits are not separate Wayland surfaces (usually)

A subunit is a logical part of its parent window, not a separate surface.
Subunit activation changes what the parent window renders.

Apps *could* represent a subunit as an actual nested surface via
`xdg-foreign-exported`, but the default model is to keep subunits logical.

## Cross-user aggregation

Per-user WMs see only their own windows and subunits. **Admin's WM
aggregates across users** — admin's launcher and window list show
everyone's tabs, panes, and cells with source-user colour tint.

## Search

A `qdistro-windows` service aggregates window + subunit state across users
for admin-launcher search:

- Search by title / metadata → matching subunits, colour-tagged.
- Click → activate the subunit in its originating session.
- Content-level search is handled by [recall](recall.md).

## Cold-start placeholder taskbar entries

When the user launches an app that lives in a not-yet-running silo
(podman container off, VM not started, cross-uid silo not yet bridged),
qdshell inserts a **placeholder taskbar entry** as immediate visual
feedback while the silo starts up. The placeholder:

- Renders with the resolved app icon (or host placeholder glyph) and
  the silo badge.
- Carries an `NBusyIndicator` overlay.
- Renders at reduced opacity (≈0.6) to distinguish from real toplevels.
- Is keyed off the launch token returned by the spawn helper, **not**
  off `app_id` — multiple instances of the same app from the same
  silo each get their own placeholder.

When `qdwin_shell_v1.toplevel_added` arrives with a
`wp_security_context_v1.instance_id` matching the launch token, the
placeholder is removed and the real toplevel takes its slot in the
taskbar model. If no match arrives within 15s, the placeholder is
removed and a toast surfaces ("Failed to start <app> in <silo>").

No placeholder *window* is created — the taskbar entry is the sole
visible feedback during cold start. The standard cursor busy/wait
feedback (provided by qdwin / the desktop environment) covers the
pointer-side cue.

This contract is identical across tiers. Tier-2 / tier-5 spawn
helpers each emit a launch token on stdout; qdshell's PodApps and
(future) VMApps services share the same token-correlation code path.
See [containers.md](containers.md#cold-start-contract) for the tier-2
implementation.
