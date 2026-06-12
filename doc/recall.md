# Recall (continuous activity capture and search)

> **Status:** planned post-v1. Recall is cut from the v1 release: capture is
> disabled, `recall.push` is not a registered browser-bridge op, and the
> release bootstrap profile does not install or enable the Recall timer/service.
> The viewer/query grant model below is retained as the bar for bringing Recall
> back after v1.

A time-indexed, searchable archive of what the user has been looking at, so
they can later search for "that thing I was reading yesterday" or "the PR I
reviewed last week." Integrated with the window manager (screenshots +
subunit events) and first-party apps + the browser extension (text content),
consuming the transport from [browser](browser.md) and the structure from
[window-hierarchy](window-hierarchy.md).

## Goal

Searchable from the admin launcher:

- Text search ("pandas groupby") → timestamps + thumbnails + window /
 subunit references.
- Time-range browse ("yesterday afternoon") → a scrollable timeline.
- Window / app / user filter ("everything in firefox work-user on
 thursday").

## Privilege separation — viewer grant

Recall data is sensitive enough to deserve its own privilege compartment,
distinct from admin's normal live session. Admin unlock proves owner presence
for the machine, but it must not make historical Recall data ambiently
readable by ordinary admin-session processes.

Recall has two principals:

- **Capture/index service** — write-only from the live session's perspective.
  It ingests screenshots, text, and structure, and cannot browse history.
- **Viewer/query service** — holds read/decrypt authority only for a
  time-boxed viewing grant after explicit re-authorization.

`recall-user` remains a useful name for the viewer principal, but it is not a
standing TTY that continuously owns read access. The viewer may be implemented
as a pinned TTY, a dedicated secure surface, or a separate service plus UI; the
security requirement is that decrypted Recall results are unavailable to the
ordinary live admin session.

Consequences:

- If admin's live session is compromised, recall should not become ambiently
 readable. Admin can request a viewer grant, but the grant is explicit,
 time-boxed, audited, and revoked on lock.
- Recall queries live in a dedicated session context; no bleed into admin's
 normal workflow.
- Admin manages the machine (users, vaults, policy); recall-user only
 *views* recall. Clear separation of duties.

### What the viewer cannot do

- Write recall data — read-only. The capture daemons are the sole source
 of truth.
- Modify app state in other users' sessions. The recall viewer can show
 "notebook-A at 14:32 yesterday" but can't jump dev-user's actual notebook
 to that state.
- See live surfaces of other users. Recall is strictly the captured
 archive, not live observation.

### The recall viewer app

A PyQt app in recall-user's session:

- Timeline view (scrollable, thumbnails).
- Text search box.
- Filter chips: user, app, date range.
- Detail pane: full captured content around the selected timestamp.

## Privacy is load-bearing

Recall is a high-value attack target.

- **Default off per user.** Admin opts in per user, never automatic.
- **Encrypted behind the viewer gate.** Encryption-at-rest alone is not enough:
 keys must be unavailable to ordinary live-session processes and released only
 through the viewer/query grant.
- **TTL** — default 30 days, admin-configurable; older captures
 auto-deleted.
- **Exclusion lists** — URLs, apps, window titles, widget kinds marked
 sensitive never get captured.
- **Password-field-aware** — the SDK flags password fields; capture skips
 them (the WM screenshot blurs them; text dump omits them).
- **Incognito detection** — the browser extension detects private browsing;
 the extension does not send page content.
- **Pwd-manager exclusion** — hard-coded: pwd-manager surfaces are never
 captured.
- **Visible indicator** — admin compositor shows a recall-active icon
 (top-right) whenever any user's recall is ingesting, matching mic/camera
 indicators.
- **Viewing re-auth** — every viewing session requires a fresh, time-boxed
 owner re-authorization and is logged.
- **Lock behavior** — admin lock revokes active viewing grants and clears
 decrypted thumbnails, snippets, query results, and viewer state.

## What is captured

Three streams per user:

1. **Screenshots** — the WM captures per-top-level-window snapshots at a
 throttled rate (e.g., on focus change, on significant visual change,
 max ~1 per second). Deduplicated by perceptual hash.
2. **Text content** — SDK-opted-in apps push textual snapshots on
 meaningful change (throttled). The browser extension pushes page text +
 URL. The terminal pushes scrollback deltas. The notebook pushes cell
 contents. Text is preferred over OCR'ing screenshots for these apps.
3. **Structural events** — focus changes, tab switches, subunit
 activations. Lightweight metadata stream used to reconstruct the
 timeline.

Capture tiers are policy-controlled: metadata only, text, screenshots, OCR,
and audio are separate opt-ins. The default should be narrow and visible.

## Architecture

```
 +----------------------------+
 | qdistro-recall@<user> | (planned per-user writer/indexer;
 | .service | writes /var/lib/qdistro/recall/<user>/)
 +-------------+--------------+
 ^
 | ingest
 |
 +-----------+---------------+------------------+
 | | |
 WM qdistro_app qdistro-browser
 (screenshots, SDK (via the bridge)
 subunit events) (text from apps) (page text, URL,
 incognito flag)
```

The WM is the only screenshot source. The SDK + browser bridge are the text
sources. Subunit tracking is the structural-event source. In v1 these capture
paths are disabled; when Recall returns, the planned service makes the write
path explicit and centralizes indexing/query policy.

## Indexing

- **Text**: full-text index (SQLite FTS) + embedding vectors for semantic
 search.
- **Screenshots**: perceptual hash for dedup; OCR'd lazily on demand.
- **Structural events**: timestamp-ordered log in SQLite.

Embeddings are generated by a local model (ollama / llama.cpp). A cloud
model is opt-in per-user, admin-approved, off by default — keeps capture
data local.

## Query API

The planned `qdistro-recall` query service exposes viewer-gated methods on
`qbus-admin`:

```
SearchText(user, query, time_range) -> list[Hit]
SearchTimeline(user, time_range) -> list[Event]
GetThumbnail(hit_id) -> image
GetContext(hit_id) -> full captured content around that timestamp
```

Each `Hit` includes the user (colour-tagged), the window title and app
identity, the subunit (tab URL / pane cwd / notebook name), the timestamp,
and a snippet matching the query.

Export is not a query method. Exporting Recall results is a declassification
workflow that records destination, labels/security changes, approval, and
lineage. See [workflows.md](workflows.md#export-and-declassification).

## UI surfaces by role

- **Viewer grant/session** (primary) — full recall viewer. All opted-in
 users, full timeline, cross-user search, filter chips, time-boxed grant.
- **Per-user launcher** (narrow) — shows only that user's own recall, and
 only if admin policy enables it. Useful for "what was I doing yesterday
 in work-user?"
- **Admin's launcher does *not* include ambient recall search by default.**
 Admin requests a viewer grant. The final UI may be a pinned TTY, secure
 surface, or separate viewer process, but decrypted results stay out of the
 normal admin workflow.

## Apps opt in via the SDK (post-v1)

```python
qdistro_app.recall.enable()
qdistro_app.recall.set_sensitivity('low' | 'medium' | 'high')
qdistro_app.recall.exclude_fields([widget_ids...])
qdistro_app.recall.push_text_snapshot(content)
```

The app decides when content has changed meaningfully and pushes. In v1 the
SDK import remains available, but capture calls fail closed. Post-v1 the SDK
handles throttling, encryption, and forwarding.

## Browser extension opt-in (post-v1)

The extension exposes a qdistro-recall preference:

- Enabled / disabled per browser per user.
- Per-domain include / exclude.
- Incognito: auto-excluded, not configurable.

The extension captures page text + URL on meaningful navigation / content
change only after Recall is reintroduced post-v1.

## Exclusion config

```yaml
# /etc/qdistro/recall-exclusions.yaml
apps:
 - /usr/bin/qdistro-pwd-ui
 - /usr/bin/keepassxc
window_titles:
 - "*Private Browsing*"
 - "*Password Safe*"
urls:
 - "*.bank.example.com"
 - "login.*"
widget_kinds:
 - password
```

Consulted by the WM, the SDK, and the bridge before ingesting.

## Storage

- Path: `/var/lib/qdistro/recall/<user>/<year-month>/`.
- Per-day SQLite DB + object storage for thumbnails.
- The master key is sealed so ordinary live-session processes cannot unwrap it.
 Decryption authority is bound to the viewer/query grant, not merely to
 "admin is unlocked."
- Daily DB rolled over; old entries auto-deleted per TTL.
- Size cap (e.g., 20GB per user) with FIFO eviction when the cap is hit.

If the disk is stolen, recall is unrecoverable without the required TPM/owner
authorization material. Key recovery, backup, and deletion semantics remain
open design items.

## Cost

- CPU: continuous capture + embedding generation is non-trivial. Plan for
 5-10% baseline CPU per heavily-active opted-in user.
- Disk: ~1GB / day per heavy-use user (thumbnails dominate).
- GPU: if using GPU-accelerated embeddings, moderate GPU load.

Not free. Opt-in per user precisely because of this cost.

## Threat-model lessons from Microsoft Recall

Microsoft's redesigned Recall documentation describes a stronger architecture
than the original public preview: opt-in capture, filtering, TPM/Windows Hello
binding, proof-of-presence for viewing, and protected storage. qdistro treats
that as vendor-described prior art, not as proof against every live-session
malware path.

qdistro's design absorbs four lessons:

1. **The decrypted-render surface is the real attack target, not the
 at-rest blob.** The viewer grant and separate viewer principal are direct
 responses: decrypted pixels, snippets, thumbnails, clipboard content, logs,
 accessibility output, and browser caches are all sensitive.
2. **Per-query re-auth, not per-session.** Argues for fingerprint or
 biometric on every viewing grant, not just at machine unlock. Annoying by
 design.
3. **Content filters at *capture* time, not at index time.** Pwd-manager
 surfaces and incognito browsing must be excluded by the *producer*
 (WM, SDK, browser bridge), not by post-hoc filtering on the index.
 The WM is the only enforcement layer that holds against a malicious
 app — SDK + bridge are advisory.
4. **Export is declassification.** Exported snapshots, snippets, and query
 results become ordinary data outside the protected store and must go through
 workflow policy, lineage, and destination labeling.
