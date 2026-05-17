# qdistro_app SDK

## Purpose

Every first-party PyQt app integrates with `qdistro_app`. The SDK is how apps
participate in qdistro features — handoff, read-only mode, viewer awareness,
clipboard policy, isolation-tier context — with minimal boilerplate.

Trusted apps honour the SDK cooperatively. Untrusted or third-party apps are
handled by coarser tier-2 mechanisms (input filtering at the nested
compositor, container sandboxing).

## Constraint — single top-level window per app

The SDK assumes each app has one top-level QWidget (modals and popups are
fine — they share the app's single `wl_display` connection and follow
wherever it goes). Multi-window apps (IDEs, browsers) are handled by running
them inside containers with nested compositors (see
[window-handoff](window-handoff.md)).

This constraint makes handoff (tear down + reconnect + rebuild) cheap.

## Integration pattern — apps are agnostic, plugins carry the SDK

qdistro does **not** require first-party apps to `import qdistro_app` at
compile time. Existing apps (e.g., qterminator, qnotebook) stay usable
outside qdistro — they expose plugin systems, and qdistro ships one plugin
per app that hooks its host into the SDK.

Each per-app plugin does three things:

1. Subclasses `AppReceiver`, claims `org.qdistro.<AppName>.uid<N>` on the
 host's session bus, and wires `Receive(kind, payload)` onto host-specific
 state (a terminal pane's PTY for qterminator; the current page's cursor
 for qnotebook).
2. Adds a "Send selection to…" context-menu entry through the host's own
 plugin API, backed by `list_receivers()` + `send_to()`.
3. Optionally exposes `GetLastReceived()` so headless tests can assert
 delivery without screen-scraping.

The SDK ships the generic side of the contract (bus-name claiming, message
dispatch, peer discovery). Per-app integration (what "read-only" means for
*this* editor, where an incoming payload *goes* in *this* app) lives in
each app's qdistro plugin — that's the only piece that understands the host.

## Standard D-Bus interface

Each SDK-using app exposes `org.qdistro.App1` on its session bus.

### Methods

```
SetReadonly(enabled: bool)
 Put the app into cooperative read-only. App should disable edit UI
 (grey edit buttons, show lock icon, disable keyboard editing).

SetViewer(username: string)
 Inform the app that it's currently being viewed by `username`
 (which may differ from the user that owns the process in view handoff).
 App should display a banner / border / label showing viewer identity.

SetIsolationTier(tier: enum)
 Inform the app of its isolation context. App may use this for
 UX decisions.

HandleHandoff(target_user: string) -> success: bool
 Migrate display to target_user's compositor.

RequestClipboardPaste(source_user: string) -> payload: variant
 Fetch clipboard content from another user with policy gating.

SendClipboardTo(target_user: string)
 Forward the app's current clipboard selection to another user via
 qbus-peer (admin-brokered).
```

### Signals

```
handoff_completed(new_compositor, new_viewer)
readonly_changed(enabled)
viewer_changed(username)
tier_changed(tier)
sensitive_device_granted(device, scope)
sensitive_device_revoked(device)
```

## Qt properties

The SDK exposes bindable Qt properties so QML can respond without imperative
code:

```python
qdistro_app.readonly # bool
qdistro_app.current_viewer # str
qdistro_app.isolation_tier # enum
qdistro_app.owning_user # str
```

Example QML:

```qml
Rectangle {
 color: qdistro_app.readonly ? "gray" : "white"
 border.color: qdistro_app.current_viewer === qdistro_app.owning_user
 ? "transparent" : qdistro_app.viewer_color
}
```

## Clipboard-policy hooks

An app can register per-window clipboard-transform hooks. Called before a
copy leaves the app (e.g., redact fields, strip MIME types):

```python
@qdistro_app.on_copy
def redact(event):
 if 'sensitive' in event.origin_widget.tags:
 event.deny()
```

## Handoff lifecycle callbacks

```python
@qdistro_app.before_handoff
def save_state():
 return {'doc_path': current_doc.path, 'cursor': current_cursor}

@qdistro_app.after_handoff
def restore_state(state):
 open_document(state['doc_path'])
 set_cursor(state['cursor'])
```

Only in-memory state survives (the process did not die); files on disk are
accessed through the same uid (the app's owning user, which did not change
— remember, it's *view* handoff).

## Read-only is cooperative

Read-only is a D-Bus flag and a UI contract, not a sandbox. If the process
is buggy or compromised, "read-only" is not enforced. Fine for first-party
trusted apps; third-party apps should not be handed off with "read-only"
alone — use nested-compositor input filtering.

## Not in the SDK

The SDK does *not* try to wrap:

- Wayland protocol directly (the compositor's job, through libweston).
- PipeWire streams (use PipeWire APIs directly).
- Polkit calls (the SDK wraps common ones via D-Bus; direct polkit calls
 stay available).
- File system access (standard Python / Qt).

Keep the SDK focused on **qdistro-specific concerns**: handoff, viewer
awareness, read-only, clipboard policy. Everything else uses normal
libraries.
