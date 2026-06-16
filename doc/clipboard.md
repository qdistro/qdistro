# Clipboard

## Wayland clipboard is compositor-scoped

This is the load-bearing fact. Each compositor manages its own `wl_data_device`
state. Clients attached to compositor X share that clipboard; clients attached
to compositor Y don't see it.

This means:

- Inside a single session, apps share a clipboard naturally.
- Inside a container with a nested compositor, the container's apps share a
 clipboard that does *not* cross to the outer.
- A waypipe-bridged app's clipboard is the clipboard of whichever compositor
 it's bridged to.

The model emerges from Wayland itself; no qdistro clipboard daemon is needed
for intra-compositor use.

## Handed-off windows — no special case

A handed-off app's clipboard naturally becomes the target compositor's
clipboard, because the app's `wl_display` (hence its `wl_data_device`) is
bridged there. Zero extra plumbing needed.

If per-window filtering is required (redact MIME, transform content, log),
the waypipe bridge itself is where hooks attach.

## Session-owned clipboard

The clipboard is a session surface, not persistent silo state. A silo
may receive a transient compatibility clipboard item while content is being
delivered into an application, but that item should be cleared after transfer.
This prevents clipboard contents from becoming hidden state that follows a silo
when it is detached from one session and reattached to another.

Clipboard history, if implemented, belongs to the session and must be treated
as sensitive data. Silos should not carry clipboard history.

## Cross-compositor transfer (non-handoff)

The interesting case: an app in one session or silo wants to send clipboard
content to another session or silo. These compositors don't share state, so
we need a bridge.

### Imperative path — context menu

The owner right-clicks the selection or uses a privileged shortcut → "Send
clipboard to..." submenu lists target sessions, silos, or other policy-defined
resources. The shortcut and menu surface are owned by qdshell / the compositor
path; local applications must not be able to intercept or spoof them.

1. A's compositor (or the app's SDK) invokes `org.qdistro.clipboard.send`
 on `qbus-admin`.
2. The broker applies policy (declarative rules + Python hooks).
3. If approved, the broker sets up an ephemeral peer socket between A's
 and B's compositors.
4. A writes the payload; B reads; the socket closes.
5. B's compositor writes the payload to its own `wl_data_device`.

### Declarative path — admin workflows

Admin authors rules and hooks that route clipboard automatically based on
source / target / MIME / content. Examples:

- "Copies from terminal-work auto-offer to terminal-review."
- "Any copy from finance-user prompts admin before leaving that silo."
- "Git SHA copied from dev-user auto-pastes into review-user's git
 clipboard."

The broker applies rules before prompting admin; only unmatched requests
prompt.

### Rich transfer UI

qdistro should support more than plain text, but every format crossing must be
explicit. Transfer UI should expose the shape of the payload rather than hiding
it behind a generic paste:

- Paste plain text.
- Paste safe Markdown.
- Paste rich text / HTML.
- Paste image.
- Paste files.
- Preview before paste.
- Edit or sanitize before paste.

The broker evaluates MIME type, source, destination, app identity, and policy
before delivery. File and rich-content transfers are higher risk than plain
text and should make that risk visible in the UI.

The default rich-text option should be a safe Markdown subset: no images, no
raw HTML, and simplified URLs where possible. Unsanitized Markdown, HTML,
images, files, and app-specific MIME formats remain available through explicit
context-menu actions when policy allows them.

Sanitization creates a tracked derivative. It does not erase lineage. A
cross-silo transfer appends lineage, conservatively unions contamination
labels, and records whether the payload was plain, sanitized, or unsanitized.

### No central clipboard daemon

qdistro deliberately avoids a "clipboard service" that holds clipboard state
across compositors. Wayland already provides per-compositor clipboards; a
central store would duplicate state, become a high-value target, and apply
policy at storage-time rather than transfer-time.

Each session/compositor clipboard is its own; transfer is the only point
where policy and brokering apply.

## Compositor-mediated gating

The set-side gate fires on every clipboard set:

- The compositor's `selection_set(seat, source_handle, mime_types,
 is_primary)` event reports each new clipboard selection, identifying the
 source by the focused-toplevel handle (Wayland only permits clients with
 keyboard focus to set selection).
- qdshell resolves the source silo from the toplevel's identity
 (tier-3 silos via title-prefix / waypipe-secctx; tier-4/5 VMs via
 `wp_security_context_v1`; admin otherwise).
- Same-silo transfers short-circuit to allow.
- Cross-silo transfers call `broker.CheckClipboardTransfer(source_silo,
 dest_silo, mime_types)`; a deny verdict triggers `clear_selection` on the
 compositor.

The complementary **focus-aware-clear** primitive ensures the silo on the
sink side only sees the clipboard while one of its own toplevels has
keyboard focus. On every focus change, qdshell clears any active selection
whose source silo differs from the newly focused silo. This is the
Qubes-style mitigation for the "admin → silo paste-receive" direction.

A finer-grained **receive-time gate** wraps `wl_data_offer.receive` and
calls `CheckClipboardReceive(source_silo, dest_silo, mime_type, source_app_id,
dest_app_id, source_sandbox_engine)`. Rules can specify `mime_type:` (with
fnmatch glob support — `text/*`, `image/*`, `application/*`) to allow or
deny specific MIME shapes per source/dest pair.

### Deny-storm robustness

The set-side gate calls `clear_selection` on **every** deny, and `clear_selection`
is a synchronous request into qdshell's fixed 4 KB libwayland output buffer to
the compositor. A producer that re-asserts a denied selection in a tight loop —
a buggy client, a clipboard manager that re-offers, or a malicious tier guest
deliberately flooding `selection_set` — would otherwise drive a deny→clear→
re-offer storm. At a high enough rate the compositor drains slower than qdshell
fills, libwayland's marshal hits `Data too big for buffer`, and the **whole
privileged shell↔compositor connection is fatally errored** — taking out the
clipboard isolation channel and silently dropping any in-flight load-bearing
request (e.g. a cross-silo focus injection) during the rebind window.

Two coordinated mitigations keep the channel alive under load:

- **qdshell coalesces redundant clears.** The first deny for a given
 denied-offer identity (`seat` + selection kind + source silo + dest silo +
 mime set) always clears; identical repeats inside a short window (~500 ms)
 suppress only the redundant *wire* call. This bounds the `clear_selection`
 rate to at most one per identity per window. It does **not** weaken
 fail-closed: every verdict is still audited, the independent set-time and
 receive-time gates still run per event, and re-clearing an already-cleared
 selection is a no-op — a denied offer that briefly persists for up to one
 window while being re-cleared is strictly safer than the channel dying and
 clearing nothing. (`ClipboardDenyCoalesce.js`.)
- **qdshell never assumes a request was sent.** After every imperative request
 it checks the `wl_display_flush` result; a fatal (non-`EAGAIN`) error tears
 the binding down and triggers reconnect rather than continuing in a false
 "bound" state. (`qdwin-binding.cpp`.)
- **The compositor makes `clear_selection` idempotent** (defense-in-depth):
 clearing an already-empty seat/kind selection is a no-op, so redundant clears
 cost nothing on the compositor side.

## Audit

Every cross-compositor transfer flows through `qbus-admin`. Admin can log:

- Source session/silo, target session/silo, timestamp.
- MIME types (not payload, by default — admin can opt into content logging).
- Policy decision.

Provides a forensic trail without being privacy-violating by default.

## MIME and content handling

Rules must handle MIME, not just plain text. Clipboards routinely carry:

- `text/plain`, `text/html`
- `image/png`, `image/svg+xml`
- `x-special/gnome-copied-files` (file URIs — be careful; transferring
 implies the target can read those paths, which may be inaccessible
 across uid boundaries)
- `application/json` and app-specific types

Policy language supports MIME glob matching. File-URI transfers across uid
boundaries either fail (default) or trigger an admin-approved file-content
read at the source with policy-controlled delivery at the target.

## Primary selection vs clipboard

Wayland distinguishes `primary` (middle-click selection) from `clipboard`
(explicit copy). qdistro applies the same policy framework to both, but
default rules differ — primary is often more ephemeral and gets less
friction.

## Per-app policy via the SDK

First-party apps can register per-window `on_copy` hooks. These run *before*
the copy leaves the app — useful for redaction, tagged fields, etc.
App-level filtering stacks on top of compositor-level policy.
