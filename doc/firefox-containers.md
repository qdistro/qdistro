# Firefox containers (contextual identities)

> **First version 2026-05-16.** Wire shape pinned, own-uid round-trip
> tested, cross-uid relay (Option B) landed. Remaining open work is
> the per-feature admin opt-in UI — tracked in the Status table.

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

## Cross-user routing

The bridge is per-user, per-browser: there is one
`org.qdistro.BrowserBridge.<ppid>` on each user's session bus. A daemon
running as **the same uid** as the Firefox process calls
`containers.*` directly through the session bus
(`org.qdistro.BrowserBridge.<ppid>.RequestTabs`).

A daemon running as **a different uid** (typically admin daemons, the
admin panel, recall ingest) reaches the same op via the user-relay's
system-bus surface (`UserRelay.ForwardBrowserBridgeOp`). Three options
were considered; the implementation choice is recorded in the
"Decision" subsection below.

### Option A — own-uid only (default per [browser.md](browser.md#cross-user-defaults))

Cross-user feature surfaces are opt-in per-feature per-user-browser.
Default: admin cannot list user X's containers. Admin must enable
"Containers in admin panel" in the per-user-browser toggle UI before
*any* admin daemon can reach `containers.list` for that
user-browser.

When enabled, the routing still has to happen — see B/C below.

### Option B — UserRelay-style narrow forward — **landed 2026-05-16 for all three ops**

`UserRelay.ForwardBrowserBridgeOp(op, args_json, selector_json) -> reply_json`
shipped in `qdistro_user_relay.py`. Cross-uid callers reach it on the
system bus (`com.qdistro.UserRelay.uid<NNNN>`); the relay forwards to
the user's `org.qdistro.BrowserBridge.<ppid>` via `RequestTabs`. Wire
matches the bridge's `RequestTabs(s, s) -> s` so callers handle relay
and direct paths identically.

`selector_json` chooses which bridge:

| Selector | Meaning |
|----------|---------|
| `{"ppid": <int>}` | Exact match on `org.qdistro.BrowserBridge.<ppid>`. Must be a JSON integer; a quoted `"1234"` is refused as `bad_selector` to surface caller serialization bugs. |
| `{"any": true}` | First bridge by **numeric ppid** (so `9999` precedes `10000`, matching operator intuition rather than lexicographic order on the full bus name). |
| `{}` | Refused with `no_bridge_found` — callers must opt in to "any" so a typo doesn't route to a random browser. |
| `{"ppid": N, "any": true}` | Refused with `bad_selector` — the caller's intent is ambiguous. |

The relay's bridge enumeration **requires the suffix after
`org.qdistro.BrowserBridge.` to be all-digits**. A same-uid attacker
that can claim `org.qdistro.BrowserBridge.impostor` on the session
bus is *not* a routing target — without this gate, `{"any": true}`
could send admin calls there.

Failures inside the relay return JSON `{"ok": false, "error":
"<code>"}` rather than D-Bus exceptions, so callers handle them
identically to bridge-side `ok:false` replies:

| `error` | When |
|---------|------|
| `missing_op` | empty `op` string |
| `bad_selector` | `selector_json` isn't a JSON object |
| `no_bridge_found` | nothing matches the selector |
| `bridge_call_failed` | bridge raised — `detail` carries the underlying message |

Authorization is the **system-bus peer-uid policy** on
`com.qdistro.UserRelay.uid<NNNN>` — same model as the existing
`UserRelay.Forward` for notifications. No new authn surface inside the
relay. Test coverage: `tests/unit/test_user_relay.py::TestForwardBrowserBridgeOp`
(12 cases).

Trade-off accepted: the relay turns into a generic browser-bridge
proxy. The narrow `Forward(kind, payload)` precedent does NOT carry
over — every bridge op is now reachable cross-uid. If a future op
needs admin sign-off, route *that op* through option C explicitly;
don't broaden the relay's gate.

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

### Decision — landed 2026-05-16

**Option B for all three ops.** The doc's earlier draft recommended
hybrid B-for-list / C-for-create+remove. After implementation review,
the broker layer adds enough operational complexity for write ops
(per-call user prompt every container create) that we shipped pure B
and left the option open to escalate writes to broker mediation if a
real abuse vector surfaces. Each user-browser still has to be opted
into "Containers in admin panel" before any cross-uid call lands;
that toggle is Option A's gate and stays in place.

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

Per [browser.md §Audit](browser.md#audit), ops that reach a daemon
are logged by that daemon. For `containers.*` the eventual daemon
audit lives in whichever consumer ends up calling it (admin panel,
recall, etc.).

The **cross-uid relay** writes one journal line per
`ForwardBrowserBridgeOp` call, format:

```
[qdistro-user-relay/audit] kind=forward_bridge_op sender=<bus-name>
    op=<op> bridge=<bridge-name-or-"-"> ok=<true|false> error=<code-or-empty>
```

Fields are space-separated `key=value` so `journalctl -u
qdistro-user-relay | grep audit` is the audit-trail. The relay does
**not** log container names, icons, or cookie_store_ids — those
belong in the bridge's own audit (not yet implemented) so they aren't
mirrored into a second log site.

The **bridge-side journal logging** described in
[browser.md §Audit](browser.md#audit) is not yet implemented for any
op; that's a separate Phase-9 follow-up tracked in
[`todo/browser/01-bridge-phase9.md`](../../todo/browser/01-bridge-phase9.md).

## Calling from a qdistro daemon

The `qdistro_browser_bridge_client` module wraps both paths so
daemons don't reinvent the jeepney boilerplate. Same-uid:

```python
from qdistro_browser_bridge_client import call_bridge

reply = call_bridge("containers.list")
# {"ok": True, "containers": [{"cookie_store_id": ..., "name": ...}, ...]}
```

Cross-uid (the qdistro-user-relay must be running as the target user
and the caller must be system-bus-authorized to send to
`com.qdistro.UserRelay.uid<NNNN>`):

```python
from qdistro_browser_bridge_client import call_via_relay

reply = call_via_relay("containers.list", uid=2000, any_bridge=True)
# {"ok": True, "containers": [...]}
# or
# {"ok": False, "error": "no_bridge_found"}      # no Firefox bridge on uid 2000
# {"ok": False, "error": "relay_call_failed"}    # relay not running / unauthorized
# {"ok": False, "error": "bad_call"}             # neither uid nor any_bridge provided
```

Both functions always return a dict; failures inside the client
become `{"ok": False, "error": "<code>"}` rather than raised
exceptions so callers handle relay-side, bridge-side, and
transport-side failures with the same code path.

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
| Bridge own-uid round-trip | **Pinned 2026-05-16** by `tests/unit/test_browser_bridge_phase9.py::TestContainersRequest`. The bridge routes `containers.*` through the existing `enqueue_inbound_request` machinery; no per-op handler is required, only the `*.reply` registrations in `DEFAULT_HANDLERS`. |
| Cross-user relay | **Landed 2026-05-16** as `UserRelay.ForwardBrowserBridgeOp` (Option B). Tests: `tests/unit/test_user_relay.py::TestForwardBrowserBridgeOp` (17 cases). |
| Daemon client helper | **Landed 2026-05-16** as `qdistro_browser_bridge_client.call_bridge` (own-uid) / `call_via_relay` (cross-uid). Tests: `tests/unit/test_browser_bridge_client.py` (23 cases). |
| First consumer | **Landed 2026-05-16**: `qdistro_recall_admin` (`recall/qdistro_recall_admin.py`) — `list_user_containers` + `list_user_tabs` building blocks, and `annotate_with_live_tabs(rows, uid)` joins historical recall rows with the user's currently-open tabs by exact URL match. Tests: `tests/unit/test_recall_admin.py` (13 cases). |
| CLI | **Landed 2026-05-16**: `qdistro-recall-admin` (`cli/qdistro_recall_admin_cli.py`) with `containers --uid N`, `tabs --uid N`, and `search <query> --uid N` subcommands. The `search` subcommand annotates each recall hit with a `[live: w<window> t<tab>]` marker when the row's URL matches an open tab, and falls back gracefully to unannotated results when the relay is unreachable. Tests: `tests/unit/test_recall_admin_cli.py` (16 cases). |
| Cross-user admin opt-in | **Not implemented.** Per-feature toggle row in the admin panel still gates whether a cross-uid call is allowed for a given user-browser. UI doesn't exist yet for any Phase-9 feature. |
| D-Bus surface | No change required — reuses the existing `RequestTabs` entry point. |
| Cross-user gate | Per-feature opt-in toggle row in the admin panel — UI doesn't exist yet for any of the Phase-9 features; this adds a "Containers" row. |
| Audit | Bridge-side journal logging follows the existing `qdistro-browser-bridge` pattern; consumer-side audit lives in whichever daemon ends up calling it. |

## Files

| Path | Purpose |
|------|---------|
| `qdfirefox-extension/src/modules/containers.js` | Extension-side dispatcher handlers |
| `qdfirefox-extension/tests/containers.test.js` | Wire-shape tests |
| `qdistro/browser_bridge/qdistro_browser_bridge.py` | `*.reply` registrations for the bridge → ext round trip |
| `qdistro/browser_bridge/qdistro_browser_bridge_client.py` | Daemon-side helper: `call_bridge` (own-uid) + `call_via_relay` (cross-uid) |
| `qdistro/user_relay/qdistro_user_relay.py` | `ForwardBrowserBridgeOp` cross-uid surface |
| `qdistro/doc/browser.md` | `containers.*` Operations entry referencing this doc |

## See also

- [browser.md](browser.md) — the parent integration doc; this doc fills
  the Firefox-containers gap in its op matrix.
- [containers.md](containers.md) — *unrelated*: tier-2 podman containers.
- [admin-approval.md](admin-approval.md) — Option C's policy substrate.
- [`qdfirefox-extension/todo/08-bridge-protocol-alignment.md`](../../qdfirefox-extension/todo/08-bridge-protocol-alignment.md)
  §"Bridge-side gaps the extensions assume work" — where the missing
  bridge handlers were first surfaced.
