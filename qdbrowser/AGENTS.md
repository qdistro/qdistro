# Agent control for qdbrowser

qdbrowser exposes the same agent-driving shape as qterminator: a local
Unix-domain JSON-RPC socket wrapped in an MCP stdio server.

## Quick start

```bash
# Launch the browser with the socket exposed:
QDBROWSER_AGENT_CONTROL=1 qdbrowser

# Talk to it from any MCP-aware harness via the stdio proxy:
qdbrowser-mcp
```

Or in `mcp.json`:

```json
{
  "mcpServers": {
    "qdbrowser": { "command": "qdbrowser-mcp" }
  }
}
```

## Socket

Default path: `$XDG_RUNTIME_DIR/qdbrowser-agent-$UID.sock`. Secured via
`SO_PEERCRED`: connections from any other UID are dropped at accept time.

Wire format: newline-delimited JSON-RPC 2.0. The same persistent
connection can issue many calls; server-initiated events (e.g.
`page_load`, `navigation`, `download_finished`) are pushed as
out-of-band envelopes with `event` set and no `id`.

## RPC methods

### Tab management

- `list_tabs() -> [{id, title, url, attached, can_go_back, can_go_forward, loading, muted, pinned, group}]`
- `open_tab(url=None, background=False) -> {id}`
- `close_tab(tab_id)`
- `attach(tab_id) -> {ok, page_load_seq}` — required before mutating verbs
- `detach(tab_id)`
- `focus_tab(tab_id)`

### Navigation

- `navigate(tab_id, url)`
- `go_back(tab_id)` / `go_forward(tab_id)` / `reload(tab_id)`
- `stop(tab_id)`
- `get_url(tab_id) -> {url, title, loading}`
- `wait_for_load(tab_id, timeout=10.0) -> {ok, url}`

### Interaction (coordinates in CSS pixels relative to the viewport)

- `click_at(tab_id, x, y, button="left", modifiers=[])`
- `dblclick_at(tab_id, x, y)`
- `move_mouse(tab_id, x, y)`
- `scroll(tab_id, dx=0, dy=0)` — pixel deltas
- `type_text(tab_id, text)` — types into the focused element
- `send_keys(tab_id, keys=[...])` — symbolic names: `enter`, `tab`, `escape`, `up`, etc.

### Introspection

- `screenshot(tab_id, full_page=False) -> {width, height, png_b64}`
- `get_dom(tab_id) -> {html}` — outerHTML
- `get_visible_text(tab_id) -> {text}` — innerText of body
- `eval_js(tab_id, script) -> {result}` — `JSON`-serializable result
- `wait_for_selector(tab_id, selector, timeout=5.0) -> {ok, found}`
- `query_selector(tab_id, selector) -> {found, rect: {x,y,w,h} | null}`

### Events (server → client, no id)

- `{"event": "page_load", "tab_id": N, "url": "..."}`
- `{"event": "navigation", "tab_id": N, "url": "..."}`
- `{"event": "download_finished", "path": "...", "url": "..."}`

## Test scenarios

`tests/integration/scenarios/` holds Python scripts that drive qdbrowser
through the agent socket and assert on journal lines + screenshots.
`tests/integration/vm/` wraps them in bats and runs against a libvirt
template VM (`QDWIN_VM_TEMPLATE`).
