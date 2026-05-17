# 02 — How to use `page.extract.request`

(Bridge-side canonical copy. The extension repos mirror this doc.)

How any qdistro daemon (compositor, agent-control, recall, notification policy, ...) can read the content of an open browser tab through the bridge.

## Wire shape

**Direction:** bridge → extension.

**Inbound op:**
```json
{
  "op": "page.extract.request",
  "request_id": "<bridge-assigned>",
  "tab_id": <number>,
  "mode": "<see below>",
  "selector": "<optional CSS selector, by_selector mode only>"
}
```

**Reply:**
```json
{
  "op": "page.extract.request.reply",
  "request_id": "<echoed>",
  "ok": true,
  "mode": "<echoed>",
  "url": "<tab's location.href>",
  "title": "<document.title>",
  "content": "<the extracted string>",
  "truncated": false,
  "matched": true            // by_selector mode only
}
```

On error: `{ok: false, error: "<code>", detail?: "..."}`.

## Modes

| Mode | What it returns |
|---|---|
| `selection` | `window.getSelection().toString()` — current selected text. Empty if no selection. |
| `visible_text` | `document.body.innerText` — DOM-rendered visible text (skips `display:none`, follows layout). Recommended default for "what is the user looking at." |
| `full_text` | `document.documentElement.textContent` — every text node including hidden ones. Use when you need exhaustive content for search/recall. |
| `outer_html` | `document.documentElement.outerHTML` — full HTML source. Heavyweight; prefer one of the text modes unless you actually need the markup. |
| `by_selector` | Returns `innerText` of the first element matching `selector`. Requires `selector` field. Reply includes `matched: bool`. |
| `title` | Just `document.title`. Cheapest. |

## Limits

- **Size cap**: 256 KB per request. Larger content is truncated; reply includes `truncated: true`.
- **One tab per call**: the daemon picks which tab to read. To enumerate tabs first, send `tabs.list`, then pick a `tab_id` and call `page.extract.request`.
- **No intent token**: this is a bridge → extension op; the bridge's identity gate on the inbound D-Bus method is the security boundary (same as `tabs.list/open/close`). The extension trusts the bridge.

## Errors

| `error` | When |
|---|---|
| `missing_tab_id` | `tab_id` wasn't a number. |
| `missing_selector` | `mode=by_selector` without a `selector`. |
| `unknown_mode` | `mode` wasn't one of the modes above. |
| `bad_selector` | `selector` failed `querySelector` syntax. |
| `executeScript_failed` | The browser refused to inject (closed tab, restricted URL like `about:`, `chrome://`, `moz-extension://`, file:// without permission). `detail` carries the underlying message. |
| `capture_returned_empty` | The script returned `undefined` (rare; usually means the tab was navigating). |
| `request_timeout` | The extension didn't reply within the bridge's `enqueue_inbound_request` timeout (default 75s; the daemon caller sees this from the bridge). |

## Calling from a daemon

The bridge exposes the request on the session bus as `org.qdistro.BrowserBridge.<ppid>` at object path `/org/qdistro/BrowserBridge`, method `RequestTabs(s op, s args_json) -> s reply_json`. The same method routes every inbound op, including `page.extract.request` — there is no separate D-Bus method for page extraction.

### Python (jeepney)

```python
import json
from jeepney import DBusAddress, new_method_call
from jeepney.io.blocking import open_dbus_connection

# ppid = the bridge's parent — the browser process. Daemons that
# already track the bridge launch know which ppid to talk to; if you
# don't, list session-bus names matching org.qdistro.BrowserBridge.*
# and pick the live one.
bus_name = f"org.qdistro.BrowserBridge.{ppid}"
addr = DBusAddress(
    "/org/qdistro/BrowserBridge",
    bus_name=bus_name,
    interface="org.qdistro.BrowserBridge",
)

with open_dbus_connection(bus="SESSION") as conn:
    args = {"tab_id": 5, "mode": "visible_text"}
    msg = new_method_call(
        addr, "RequestTabs", "ss",
        ("page.extract.request", json.dumps(args)),
    )
    reply = conn.send_and_get_reply(msg)
    data = json.loads(reply.body[0])
    if data.get("ok"):
        print(f"{data['title']} @ {data['url']}\n{data['content']}")
    else:
        print(f"error: {data['error']}")
```

### Shell (busctl)

```bash
busctl --user call \
    org.qdistro.BrowserBridge.${BRIDGE_PPID} \
    /org/qdistro/BrowserBridge \
    org.qdistro.BrowserBridge \
    RequestTabs ss \
    "page.extract.request" \
    '{"tab_id":5,"mode":"by_selector","selector":"main article"}'
```

## Patterns

### "What is the user looking at right now?"

```python
# 1. Find the focused tab via tabs.list (active:true)
tabs_reply = _call("tabs.list", {})
active = next(t for t in tabs_reply["tabs"] if t.get("active"))

# 2. Read its visible text
content = _call("page.extract.request",
                {"tab_id": active["id"], "mode": "visible_text"})

# content["content"] now holds what the user sees on screen.
```

### "Recall: snapshot every tab's title + first 4KB"

```python
for tab in _call("tabs.list", {})["tabs"]:
    snap = _call("page.extract.request",
                 {"tab_id": tab["id"], "mode": "visible_text"})
    recall_db.insert(url=snap["url"], title=snap["title"],
                     excerpt=snap["content"][:4096])
```

### "Read just the article body"

```python
snap = _call("page.extract.request", {
    "tab_id": active_tab_id,
    "mode": "by_selector",
    "selector": "main, article, [role=main]",
})
if snap["matched"]:
    summarize(snap["content"])
```

## Security model

- The browser is the user's process; reading its tabs is *not* a privilege escalation — anything the bridge can see, the user can already see in their own browser window.
- The threat the bridge gate addresses is *cross-uid* abuse: a daemon running as a different uid (admin daemons, system services) calling into a user's bridge to snoop. The bridge's `_identity_gate` checks the calling D-Bus peer's selinux context and rejects non-allowlisted callers before the inbound op leaves the bridge.
- The extension itself does no caller-identity check — it trusts the bridge. Add one if and when the bridge gains a multi-tenant inbound surface.
- Page content can contain credentials, session tokens, PII. Treat the reply as sensitive: don't log to plaintext journals, don't write to user-readable disk locations, don't ship to remote endpoints without a separate user-touch attestation.

## Permissions in the extension manifest

Already covered by the existing `scripting` + `<all_urls>` host permissions. No manifest change needed; the dispatcher entry registered automatically once `src/modules/pageExtract.js` is loaded.

## See also

- `../../../qdfirefox-extension/todo/09-page-extract-request-usage.md` — Firefox-side copy.
- `../../../qdchrome-extension/todo/08-page-extract-request-usage.md` — Chromium-side copy.
- `tests/unit/test_browser_bridge_phase9.py::TestPageExtractRequest` — bridge-side round-trip + by-selector + extension-error + timeout tests.
