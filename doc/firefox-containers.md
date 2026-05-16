# Firefox containers (contextual identities)

> **Draft, 2026-05-16.** This document pins the wire shape so the
> qdfirefox-extension's `containers` module isn't dead code, and forces
> the still-open cross-user routing question into the open. Sections
> marked *Open* below name the unresolved design questions.

Firefox's **Multi-Account Containers** primitive (`browser.contextualIdentities`)
gives each Firefox profile a set of colour/icon-tagged cookie stores.
A tab opened in container *Personal* sees a different cookie jar than a
tab opened in container *Work* — useful for keeping separate Google
identities, work vs. personal logins, throwaway accounts, etc. Chromium
has no analogue; this is the load-bearing reason qdfirefox-extension is
a distinct repo from qdchrome-extension instead of a build target.

> **Disambiguation.** Don't confuse this with
> [containers.md](containers.md), which covers qdistro's tier-2 podman
> *isolation* containers (a different concept that happens to share the
> word). Firefox containers are a within-browser cookie-store
> partition; podman containers are an OS-level uid/namespace boundary.

## Why surface this in qdistro

Two consumer flows make Firefox containers worth a bridge op:

1. **Open URL in a chosen container.** A daemon ("share this link to
   work-user's *Finance* container") needs the list of available
   containers + a way to call `tabs.open` with the matching
   `cookie_store_id`. The extension already accepts
   `tabs.open({url, cookie_store_id})`; only the enumeration side is
   missing from the bridge.
2. **Container-scoped cookie export.** `cookies.export` already accepts
   a `cookie_store_id` field. A daemon wants to drive a per-container
   audit ("export the *Banking* container's cookies for the recall
   index") which again needs the list of containers up front.

Both flows depend on container *enumeration* (`containers.list`). The
create/remove ops are weaker-justified: they're convenient for an admin
panel that wants to provision a fresh container for a new workflow, but
no daemon strictly needs them today.

## Wire shape (bridge → ext)

Direction matches the rest of the inbound surface (`tabs.*`,
`page.extract.request`): the bridge is the initiator, the extension
processes the request and replies. Already implemented in
`qdfirefox-extension/src/modules/containers.js`; the bridge handlers
do not exist yet.

### `containers.list`

```json
// inbound
{"op": "containers.list", "request_id": "<bridge-assigned>"}

// reply
{
  "op": "containers.list.reply",
  "request_id": "<echoed>",
  "ok": true,
  "containers": [
    {
      "cookie_store_id": "firefox-container-1",
      "name": "Personal",
      "color": "blue",
      "color_code": "#37adff",
      "icon": "fingerprint",
      "icon_url": "resource://usercontext-content/fingerprint.svg"
    },
    ...
  ]
}
```

Error reply when the browser is Chromium / contextualIdentities is
absent: `{ok: false, error: "contextualIdentities_unavailable", containers: []}`.

### `containers.create`

```json
// inbound
{
  "op": "containers.create", "request_id": "<bridge-assigned>",
  "name": "Banking", "color": "red", "icon": "dollar"
}

// reply
{"op": "containers.create.reply", "request_id": "<echoed>",
 "ok": true, "container": { /* same shape as containers.list entries */ }}
```

`name` defaults to `"qdistro"`, `color` to `"blue"`, `icon` to
`"fingerprint"` when omitted (extension-side default).

### `containers.remove`

```json
// inbound
{"op": "containers.remove", "request_id": "<bridge-assigned>",
 "cookie_store_id": "firefox-container-3"}

// reply
{"op": "containers.remove.reply", "request_id": "<echoed>",
 "ok": true, "container": { /* the removed entry */ }}
```

Errors: `missing_cookie_store_id` (extension-side validation),
`contextualIdentities_unavailable` (non-Firefox).

The reply echoes the deleted container's metadata — useful for an
"undo" UX that wants to recreate it.

## D-Bus exposure on the bridge

The bridge's `RequestTabs(s op, s args_json) -> s reply_json` method
already routes every inbound op through one entry point (see
[`todo/browser/02-page-extract-request-usage.md`](../../todo/browser/02-page-extract-request-usage.md)).
The three `containers.*` ops add no new D-Bus surface — they just need
`_handle_containers_list / _create / _remove` in
`qdistro_browser_bridge.py`, mirroring the existing
`_handle_tabs_list / _open / _close`.

## Cross-user routing — *Open*

The bridge is per-user, per-browser: there is one
`org.qdistro.BrowserBridge.<ppid>` on each user's session bus. A daemon
running as **the same uid** as the Firefox process can call
`containers.list` directly through the session bus today (modulo the
bridge gap).

The unresolved question is whether a daemon running as **a different
uid** (typically admin daemons, the admin panel, recall ingest) gets
to enumerate another user's Firefox containers. There is no design doc
that resolves this; the existing options are:

### Option A — own-uid only (default per [browser.md](browser.md#cross-user-defaults))

Cross-user feature surfaces are opt-in per-feature per-user-browser.
Default: admin cannot list user X's containers. Admin must enable
"Containers in admin panel" in the per-user-browser toggle UI before
*any* admin daemon can reach `containers.list` for that
user-browser.

When enabled, the routing still has to happen — see B/C below.

### Option B — UserRelay-style narrow forward

Reuse the existing `qdistro_user_relay.py` pattern. Today UserRelay
only exposes `Forward(kind, payload)` for broker → user-session
notifications. Extend with (or add a sibling daemon for) a narrow
`ForwardBrowserBridgeOp(op, args_json) -> reply_json` that the broker
(root, system bus) calls on behalf of any allowlisted caller.

Pros: matches the existing pattern; the auth gate is the broker's
existing rules engine; system-bus → session-bus crossing is already
solved.

Cons: each new bridge op needs a UserRelay routing rule. The
container-add surface is wide enough (every `tabs.*`, `cookies.*`,
etc.) that the relay turns into a generic browser-bridge proxy.

### Option C — broker-mediated per-call admin approval

Every cross-uid `containers.*` call goes through the admin broker as a
distinct approvable action (matching the `admin-approval.md` pattern
for cross-uid data movement). Admin sees a prompt: "qdistro-recall
wants to list work-user/firefox containers."

Pros: most conservative; aligns with the "cross-uid data movement is
opt-in" principle in `broker/qdistro_admin_broker.py` and
[threat-model.md](threat-model.md).

Cons: each container enumeration is a user interaction. Untenable for
flows that want to populate a "send to container…" submenu on every
right-click.

### Recommendation (subject to your call)

**B for `containers.list`** (read-only enumeration, low blast radius,
high UX cost of a per-call prompt), **C for `containers.create` and
`.remove`** (write ops; admin should sign off on creating or destroying
a cross-user data partition). Either way, **A is the default until the
admin toggle is flipped**.

If you disagree, the next step is filling out the routing choice and
the dedicated daemon name (something like `qdistro-browser-relay` if
it ends up being more than a UserRelay extension).

## Security model

- Inside a single uid, listing Firefox containers is **not** a
  privilege escalation — anything the bridge can see, the user can see
  in their own Firefox UI. Same threat-model as `tabs.list`.
- The cross-uid surface (Options B/C above) is where the real boundary
  lives. The bridge itself does no caller-identity check inside the
  session bus; the system-bus crossing is the gate.
- Container *creation* with attacker-controlled name/color could be
  used for confusion attacks ("create a container named *Banking* with
  an identity-stealing add-on context-menu entry"). Container creation
  has no add-on bypass, so this is a UX concern only — but it's worth
  flagging for the cross-user write path that an unsuspecting user
  might find their container list rearranged by a misbehaving daemon.

## Audit

Per [browser.md §Audit](browser.md#audit), ops that reach a daemon are
logged by that daemon. For `containers.*`, the daemon is the
hypothetical consumer (admin panel, recall, etc.); the bridge itself
logs the inbound op + reply summary to the journal with tag
`qdistro-browser-bridge` (caller_uid, op, ok/error, container count).
Container metadata (names, icons) is not sensitive enough to redact;
log it verbatim.

The cross-uid path adds a second log site — the UserRelay or broker
records the admin → user-bridge call exactly as it does for other
cross-uid operations.

## Integration with existing ops

Two ops already accept a container id; their handling is
forward-compatible regardless of which routing option above wins:

- **`tabs.open({url, cookie_store_id})`** — pins the new tab to the
  named container. Already implemented end-to-end (extension +
  bridge).
- **`cookies.export({url, cookie_store_id})`** — scopes the export to
  cookies in the named container. The bridge's
  `_handle_cookies_export` ignores unknown fields today; the field is
  forward-compatible and lands as a behavioural change as soon as the
  bridge starts honouring it.

## Status

| Layer | State |
|-------|-------|
| qdfirefox-extension | `containers.list/.create/.remove` dispatcher handlers implemented + tested (`tests/containers.test.js`). Module is currently dead — no inbound caller. |
| qdfirefox-extension `tabs.open` | Accepts `cookie_store_id` end-to-end. |
| qdfirefox-extension `cookies.export` | Sends `cookie_store_id`; bridge ignores it. |
| Bridge `_handle_containers_*` | **Not implemented.** Blocked on the routing decision above. |
| D-Bus surface | No change required — reuses the existing `RequestTabs` entry point. |
| Cross-user gate | Per-feature opt-in toggle row in the admin panel — UI doesn't exist yet for any of the Phase-9 features; this adds a "Containers" row. |
| Audit | Bridge-side journal logging follows the existing `qdistro-browser-bridge` pattern; consumer-side audit lives in whichever daemon ends up calling it. |

## Files

| Path | Purpose |
|------|---------|
| `qdfirefox-extension/src/modules/containers.js` | Extension-side dispatcher handlers |
| `qdfirefox-extension/tests/containers.test.js` | Wire-shape tests |
| `qdistro/browser_bridge/qdistro_browser_bridge.py` | Adds `_handle_containers_*` |
| `qdistro/doc/browser.md` | Add Phase 9? section once the routing decision lands |

## See also

- [browser.md](browser.md) — the parent integration doc; this doc fills
  the Firefox-containers gap in its op matrix.
- [containers.md](containers.md) — *unrelated*: tier-2 podman containers.
- [admin-approval.md](admin-approval.md) — Option C's policy substrate.
- [`qdfirefox-extension/todo/08-bridge-protocol-alignment.md`](../../qdfirefox-extension/todo/08-bridge-protocol-alignment.md)
  §"Bridge-side gaps the extensions assume work" — where the missing
  bridge handlers were first surfaced.
