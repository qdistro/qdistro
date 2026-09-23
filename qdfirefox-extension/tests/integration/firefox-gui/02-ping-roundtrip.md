# 02 — Ping round-trip via popup

## Goal

Prove that clicking **Ping** in the popup produces a `qdistro.ping` → `qdistro.ping.reply` round-trip with the correct request-id correlation, and that the popup renders the reply payload.

This depends on scenario 01 (port-connected smoke). If 01 FAILs, skip this one.

## Preconditions

- `deploy.sh` has been run.
- Firefox is launched per scenario 01's Setup (web-ext run against the unpacked extension).
- The popup is open or can be re-opened from the toolbar action.

## Setup

Identical to scenario 01 — kill stragglers, journal-rotate, launch Firefox, wait 8s.

## Steps

1. **Click the qdistro toolbar action** to open the popup. screenshot `before-ping.png`.

2. **Click the `Ping` button.** OCR `before-ping.png` for the Ping button's bounding box and click its centre.

3. Wait 2 seconds for the round-trip + popup render.

4. **screenshot after-ping.png** — the popup's `<pre id="out">` element should now contain a JSON-rendered response. Expected shape (whitespace-tolerant):
   ```json
   {
     "ok": true,
     "response": {
       "op": "qdistro.ping.reply",
       "ok": true,
       "stub": true,
       ...
     }
   }
   ```

5. **journal — proof of the wire exchange:**
   ```bash
   $VMEXEC $VM "journalctl --identifier=qdistro-stub-bridge --since='2 minutes ago' --no-pager | tail -40"
   ```

## Assertions

- [ ] **Popup `out` pane shows `ok: true`.** OCR `after-ping.png`; the `<pre>` body contains the substring `"ok": true`. Both keys (`ok`, `response`) must be visible.
- [ ] **Popup `out` pane shows `qdistro.ping.reply`.** OCR same screenshot includes the literal string `qdistro.ping.reply` — proves the dispatcher correlated the reply by `request_id`.
- [ ] **Popup `out` pane shows `"stub": true`.** Proves the stub host (not some other endpoint) generated the reply.
- [ ] **Popup status remains `connected`.** OCR — the word `connected` (green) is still next to `Port:`. A flip to `disconnected` between steps 1 and 4 is FAIL.
- [ ] **Journal shows `recv op=qdistro.ping`.** Proves the extension's outbound request reached the host.
- [ ] **Journal shows `reply qdistro.ping.reply request_id=N`.** Proves the host generated a reply with a numeric request_id (not null/None).
- [ ] **request_id is the same on send and reply.** Parse both journal lines for the request_id; they must match. (The host echoes the id it received, so divergence here is a host bug; identity here is the protocol's correlation guarantee.)

## Teardown

Same as scenario 01.
