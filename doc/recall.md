# Recall (continuous activity capture and search)

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

## Privilege separation — `recall-user` (provisional)

Recall data is sensitive enough to deserve its own privilege compartment,
distinct from admin's TCB. Rather than admin directly reading recall,
qdistro introduces a dedicated **`recall-user`** role.

This section is provisional. `recall-user` is a special system role, not a
regular data silo, and its exact relation to the admin-only lock/unlock model is
still open.

- **Distinct uid**, created by admin. Not a regular data silo; a reader
 role.
- **Read-only access** to `/var/lib/qdistro/recall/*` across all users
 (enforced via group membership + filesystem ACLs). Only
 `qdistro-recall@<user>.service` writes; only `recall-user` reads.
- **Lives on its own pinned TTY** following the TTY-session model. A likely
 slot is **tty5+** because tty4 is reserved for the fallback desktop, but the
 specific allocation is one instance of a broader "pinned special-role session"
 pattern.
- **Own compositor, not nested in admin's.** While recall-user's TTY is
 active, admin's compositor isn't displaying — so a compromised admin
 session *can't* passively snoop recall queries in progress.
- **Authentication model open** — earlier drafts gave recall-user its own
 authentication. Current single-tenant lock design keeps unlock authority in
 admin. The final recall flow must reconcile recall privacy with the rule that
 non-admin sessions should not hold admin/root unlock credentials.
- **Switching in = "open the time machine"** — the intended UX is a clear,
 dedicated recall context, but the final TTY, unlock, and lifecycle semantics
 are open.

Consequences:

- If admin's live session is compromised, recall should not become ambiently
 readable. The exact mechanism is open because recall privacy must be reconciled
 with the admin-only unlock path.
- Recall queries live in a dedicated session context; no bleed into admin's
 normal workflow.
- Admin manages the machine (users, vaults, policy); recall-user only
 *views* recall. Clear separation of duties.

### What recall-user cannot do

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
- **Encrypted at rest**, keyed to admin fingerprint + TPM (same sealing as
 password vaults). Unreadable when admin is locked.
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

## Architecture

```
 +----------------------------+
 | qdistro-recall@<user> | (per-user; systemd --user;
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
sources. Subunit tracking is the structural-event source.

## Indexing

- **Text**: full-text index (SQLite FTS) + embedding vectors for semantic
 search.
- **Screenshots**: perceptual hash for dedup; OCR'd lazily on demand.
- **Structural events**: timestamp-ordered log in SQLite.

Embeddings are generated by a local model (ollama / llama.cpp). A cloud
model is opt-in per-user, admin-approved, off by default — keeps capture
data local.

## Query API

`qdistro-recall` exposes on `qbus-admin`:

```
SearchText(user, query, time_range) -> list[Hit]
SearchTimeline(user, time_range) -> list[Event]
GetThumbnail(hit_id) -> image
GetContext(hit_id) -> full captured content around that timestamp
```

Each `Hit` includes the user (colour-tagged), the window title and app
identity, the subunit (tab URL / pane cwd / notebook name), the timestamp,
and a snippet matching the query.

## UI surfaces by role

- **recall-user's session** (primary) — full recall viewer. All opted-in
 users, full timeline, cross-user search, filter chips.
- **Per-user launcher** (narrow) — shows only that user's own recall, and
 only if admin policy enables it. Useful for "what was I doing yesterday
 in work-user?"
- **Admin's launcher does *not* include recall search by default.** Admin
 accesses recall by VT-switching to recall-user's pinned TTY. Keeps admin's
 TCB-grade session out of the recall-data read path.

## Apps opt in via the SDK

```python
qdistro_app.recall.enable()
qdistro_app.recall.set_sensitivity('low' | 'medium' | 'high')
qdistro_app.recall.exclude_fields([widget_ids...])
qdistro_app.recall.push_text_snapshot(content)
```

The app decides when content has changed meaningfully and pushes. The SDK
handles throttling, encryption, and forwarding.

## Browser extension opt-in

The extension exposes a qdistro-recall preference:

- Enabled / disabled per browser per user.
- Per-domain include / exclude.
- Incognito: auto-excluded, not configurable.

The extension captures page text + URL on meaningful navigation / content
change.

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
- The master key is derived from admin fingerprint + TPM. Unlocked only
 when admin is unlocked.
- Daily DB rolled over; old entries auto-deleted per TTL.
- Size cap (e.g., 20GB per user) with FIFO eviction when the cap is hit.

If the disk is stolen, recall is unrecoverable without admin's live
fingerprint + TPM.

## Cost

- CPU: continuous capture + embedding generation is non-trivial. Plan for
 5-10% baseline CPU per heavily-active opted-in user.
- Disk: ~1GB / day per heavy-use user (thumbnails dominate).
- GPU: if using GPU-accelerated embeddings, moderate GPU load.

Not free. Opt-in per user precisely because of this cost.

## Threat-model lessons from Microsoft Recall

The 2024 Recall launch was pulled within weeks: the SQLite store and
screenshots were plaintext on disk, readable by any malware running as the
user. The 2025 relaunch added a VBS enclave, TPM-bound keys tied to
Windows-Hello Enhanced Sign-in Security, proof-of-presence (re-auth on
every query), opt-in only, anti-hammering rate limits, content filters,
and Pluton on supported SKUs. The enclave holds. The **renderer process
does not** — DLL injection into the user-context renderer pulls
already-decrypted snapshots through legitimate COM.

qdistro's design absorbs three lessons:

1. **The decrypted-render surface is the real attack target, not the
 at-rest blob.** "recall-user on its own pinned tty + own compositor"
 is a direct response: admin malware can't observe decrypted recall
 pixels because admin's compositor isn't even rendering while
 recall-user is active.
2. **Per-query re-auth, not per-session.** Argues for fingerprint or
 biometric on every search, not just at VT-switch time.
 Annoying-by-design.
3. **Content filters at *capture* time, not at index time.** Pwd-manager
 surfaces and incognito browsing must be excluded by the *producer*
 (WM, SDK, browser bridge), not by post-hoc filtering on the index.
 The WM is the only enforcement layer that holds against a malicious
 app — SDK + bridge are advisory.
