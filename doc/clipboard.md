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

> **Status for this whole section.** What ships is the *implicit* path: an
> ordinary copy-and-paste between silos, intercepted and gated by the compositor
> at set time and at receive time. That mechanism is real, and is described under
> "Compositor-mediated gating" below. **Everything in the three subsections that
> follow — an explicit "Send clipboard to…" flow, admin prompting on clipboard
> policy, and a rich transfer/sanitization UI — is design, not shipped code.**
> The design is retained here because it is where the feature is going; nothing
> in it is available on a v1 machine.

### Imperative path — context menu (planned)

The intent: the owner right-clicks the selection or uses a privileged shortcut →
a "Send clipboard to..." submenu lists target sessions, silos, or other
policy-defined resources. The shortcut and menu surface would be owned by
qdshell / the compositor path, so local applications cannot intercept or spoof
them.

1. A's compositor (or the app's SDK) invokes `org.qdistro.clipboard.send`
 on the broker.
2. The broker applies policy (declarative rules + Python hooks).
3. If approved, the broker sets up an ephemeral peer socket between A's
 and B's compositors.
4. A writes the payload; B reads; the socket closes.
5. B's compositor writes the payload to its own `wl_data_device`.

**None of this exists.** `org.qdistro.clipboard.send` is never dispatched by
anything — its only occurrences in the tree are an example string in the hook
executor's name-mangling docstring and a usage example.
`deploy/hooks/example_clipboard_policy.py` defines an `on_clipboard_send`
handler, but nothing ever calls it (and the hook executor is not installed at
all — see [permissions.md](permissions.md)). There is no such menu in qdshell
and no ephemeral peer socket anywhere.

### Declarative path — admin workflows (partly planned)

Admin authors rules that route clipboard decisions based on source / target /
MIME. Rules **are** real for allow/deny on cross-silo transfer and receive; the
content-driven and auto-routing examples are not:

- "Copies from terminal-work auto-offer to terminal-review." — planned; there
 is no offer/route action, only allow/deny.
- "Any copy from finance-user prompts admin before leaving that silo." —
 **not possible.** See below.
- "Git SHA copied from dev-user auto-pastes into review-user's git
 clipboard." — planned; requires hooks, which are not installed.

> **The default is deny, not prompt — the opposite of what this page used to
> say.** There is no prompt path on any clipboard gate.
> `CheckClipboardTransfer`'s own docstring says it returns allow or deny and
> "never `unknown`", because the user is mid-flow and a synchronous answer is
> required. A cross-silo transfer with no matching rule is **hard-denied** and
> audited as `clipboard_default_deny`. Admin is never asked at transfer time;
> admin's only lever is a pre-authored rule.
>
> This is fail-closed, so it is safer than the documented behaviour — but it is
> also *silently* fail-closed, and an operator who expects a prompt will read a
> denied paste as a bug.

### Rich transfer UI (planned)

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

The broker would evaluate MIME type, source, destination, app identity, and
policy before delivery. File and rich-content transfers are higher risk than
plain text and should make that risk visible in the UI.

The default rich-text option should be a safe Markdown subset: no images, no
raw HTML, and simplified URLs where possible. Unsanitized Markdown, HTML,
images, files, and app-specific MIME formats would remain available through
explicit context-menu actions when policy allows them.

> **None of this UI exists, and there is no sanitizer.**
> `qdshell/Services/Keyboard/ClipboardService.qml` is a local `cliphist` history
> browser that explicitly disclaims any role in cross-silo transfers. There is
> no format chooser, no preview step, no edit-before-paste, and no Markdown or
> HTML sanitization anywhere in the tree. A cross-silo paste that policy allows
> delivers the payload **as-is, in whatever MIME formats the source offered**.
> Do not rely on "safe Markdown by default" — it is not a default that exists.

Sanitization is intended to create a tracked derivative rather than erase
lineage: a cross-silo transfer appends lineage and conservatively unions
contamination labels. The lineage half is real, with two caveats — it is
recorded on the **receive** path only, and only when `lineage_enforce` is
enabled, which **defaults off** (see [lineage.md](lineage.md)). On a stock
install a cross-silo paste writes no lineage row. The plain/sanitized/
unsanitized provenance field does not exist in any schema.

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
- qdshell resolves the source silo from the toplevel's identity. There is
 **one** mechanism, not two: the `wp_security_context_v1` `app_id` prefix, for
 both tier-3 and tier-4/5. The window title is delivered on `toplevel_added`
 and deliberately discarded — `SiloChrome.js` states that the secctx app_id is
 "the ONLY trusted source of a silo identity", precisely because a title is
 client-controlled and spoofable. Earlier wording here described a
 title-prefix path for tier-3; no such path exists, and it would be unsafe.
- When no secctx identity resolves, the fallback is `"uid:<ownerUid>"` or, when
 even that is unavailable, the literal `"unknown"` — and `"unknown"` is
 **hard-denied**. This is fail-closed. Earlier wording here said the fallback
 was "admin otherwise", which would have been fail-*open*: it would have given
 an unidentifiable window admin's clipboard reach.
- Same-silo transfers short-circuit to allow only after qdshell has verified
 the endpoint identities; when `LINEAGE_ENFORCE` is on, the broker also
 verifies the source pid/starttime against a launch record before taking
 the shortcut.
- Cross-silo transfers call `broker.CheckClipboardTransfer(...)`; a deny
 verdict triggers `clear_selection` on the compositor. The real signature is
 9-arity — `(source_silo, dest_silo, mime_types, source_app_id, dest_app_id,
 source_sandbox_engine, identity_verified, source_pid, source_starttime)` —
 not the 3-arity form this page used to show.
- **`mime_type` matching is receive-side only.** `CheckClipboardReceive` passes
 the mime into the rule matcher; `CheckClipboardTransfer` does not, so the
 mime types recorded at *set* time are audit-only and a `mime_type:` selector
 has no effect on a set-time rule. Write MIME-shaped policy against the
 receive gate.

The complementary **focus-aware-clear** primitive ensures the silo on the
sink side only sees the clipboard while one of its own toplevels has
keyboard focus. On every focus change, qdshell clears any active selection
whose source silo differs from the newly focused silo. This is the
Qubes-style mitigation for the "admin → silo paste-receive" direction.

A finer-grained **receive-time gate** wraps `wl_data_offer.receive` and calls
`CheckClipboardReceive(source_silo, dest_silo, mime_type, source_app_id,
dest_app_id, source_sandbox_engine, identity_verified, source_pid,
source_starttime)`. Rules can specify `mime_type:` (with fnmatch glob support —
`text/*`, `image/*`, `application/*`) to allow or deny specific MIME shapes per
source/dest pair.

**This broker-backed receive gate covers the regular clipboard only.** Primary
selection has its own, separate receive gate implemented inside the compositor
— stricter, and with no rule path. See "Primary selection vs clipboard" below.

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

Every gated cross-silo clipboard decision is audited by the broker. Each row
carries:

- Source silo, destination silo, timestamp.
- MIME types.
- Policy decision, with the verdict source. Set-side labels are
 `clipboard_same_silo` / `clipboard_same_silo_verified`, `clipboard_rule`,
 `clipboard_default_deny`; receive-side labels mirror them
 (`clipboard_receive_same_silo`, `..._same_silo_verified`,
 `clipboard_receive_rule`, `clipboard_receive_default_deny`, plus
 `clipboard_receive_lineage_deny`). Each row also carries the lineage reason
 and the `secctx_provenance` tag (`launcher_gated` or `advisory`), so a
 decision made without launcher-gated identity is filterable.

Payloads are **never** logged, and there is no opt-in to log them. This is
structural rather than a setting: `qdistro_admin_audit.py` has no content or
payload field, and clipboard bytes never reach the broker at all — only the
compositor moves them. Earlier wording here offered "admin can opt into content
logging"; no such switch exists, and building one would require plumbing
payloads into the broker that deliberately is not there.

The result is a forensic trail of *decisions*, not of content.

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
(explicit copy).

Both are gated, at both set time and receive time — but **not by the same
mechanism, and the defaults differ in the opposite direction from what this
page used to claim.**

**Set time — shared mechanism, and rules cannot tell the two apart.** The
compositor reports `is_primary` on `selection_set`, and qdshell tracks primary
and clipboard as separate per-seat selection kinds, clearing each independently
(`clearSelection(seat, isPrimary)`). But the broker call it makes is the same
one for both: `checkClipboardTransfer` has no `is_primary` parameter and the
synthetic action string `qdistro.clipboard.transfer:<src>:<dst>` carries no
discriminator. **No rule can match on primary vs clipboard**, so a deliberate
difference in set-time defaults is not currently expressible; one rule governs
both.

**Receive time — different mechanisms, and primary is the stricter one.** The
regular clipboard goes through the broker (`CheckClipboardReceive`, per-MIME,
rule-capable, so an admin rule can allow a cross-silo paste). Primary selection
does not reach the broker at all: `qdwin_primary_offer_receive` compares the
source and destination clients' security contexts directly via
`qdwin_primary_same_silo` and **hard-denies any cross-silo receive**, closing
the fd so the receiver sees an empty paste. That check fails closed on
incomplete identity (one client tagged and the other not, or a missing
engine/app_id, is treated as cross-silo), and two untagged admin-uid clients
count as same-silo.

**What this means for policy.** Cross-silo primary paste is **default-deny with
no rule override** — an admin cannot open it the way they can open a cross-silo
clipboard paste, because there is no broker call to write a rule against. So
"qdistro applies the same policy framework to both, but default rules differ —
primary is often more ephemeral and gets less friction" is wrong twice over:
the frameworks are different, and primary gets *more* friction, not less. A
broker-rule allow path for cross-silo primary is a possible future addition,
noted as such in the compositor source; default-deny is the deliberate secure
baseline.

## Per-app policy via the SDK

**Planned — no SDK clipboard hook exists.** The design is that first-party apps
register per-window `on_copy` hooks that run *before* the copy leaves the app,
for redaction, tagged fields, and similar, stacking on top of compositor-level
policy. The `qdistro_app` SDK has no `on_copy` function and no clipboard
surface at all, so today all clipboard policy is compositor- and broker-side.
