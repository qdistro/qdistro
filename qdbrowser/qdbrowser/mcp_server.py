"""MCP stdio wrapper around the agent_control Unix socket.

Mirrors qterminator/mcp_server.py: a thin proxy that holds one
persistent connection and re-exports each RPC method as an MCP tool.

Add to ``mcp.json``:

    {"mcpServers": {"qdbrowser": {"command": "qdbrowser-mcp"}}}

Socket path: ``--socket /path`` > ``$QDBROWSER_AGENT_SOCKET`` >
``$XDG_RUNTIME_DIR/qdbrowser-agent-$UID.sock``.
"""

import argparse
import json
import os
import socket
import threading
from typing import Any


def default_socket_path() -> str:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(runtime_dir, f"qdbrowser-agent-{os.getuid()}.sock")


class AgentControlClient:
    """Thread-safe persistent JSON-RPC client over a Unix socket."""

    def __init__(self, socket_path: str):
        self._path = socket_path
        self._conn: socket.socket | None = None
        self._buf = b""
        self._next_id = 1
        self._lock = threading.Lock()

    @property
    def socket_path(self) -> str:
        return self._path

    def _ensure(self):
        if self._conn is not None:
            return
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self._path)
        self._conn = s
        self._buf = b""
        self._handshake()

    def _handshake(self):
        exe = os.readlink(f"/proc/{os.getpid()}/exe")
        msg = {"op": "handshake", "exe": exe, "pid": os.getpid(), "id": 0}
        self._conn.sendall((json.dumps(msg) + "\n").encode("utf-8"))
        line = self._read_line()
        if not line.strip():
            return
        reply = json.loads(line.decode("utf-8"))
        if reply.get("error"):
            raise RuntimeError(f"handshake failed: {reply['error']}")

    def _read_line(self) -> bytes:
        while b"\n" not in self._buf:
            chunk = self._conn.recv(65536)
            if not chunk:
                raise RuntimeError("agent_control connection closed")
            self._buf += chunk
        line, _, rest = self._buf.partition(b"\n")
        self._buf = rest
        return line

    def call(self, method: str, **params) -> Any:
        with self._lock:
            self._ensure()
            rid = self._next_id
            self._next_id += 1
            req = {"jsonrpc": "2.0", "id": rid,
                   "method": method, "params": params}
            payload = (json.dumps(req) + "\n").encode("utf-8")
            try:
                self._conn.sendall(payload)
            except OSError:
                self._conn = None
                self._ensure()
                self._conn.sendall(payload)
            while True:
                line = self._read_line()
                if not line.strip():
                    continue
                msg = json.loads(line.decode("utf-8"))
                if msg.get("id") != rid:
                    continue  # skip events
                if "error" in msg:
                    err = msg["error"]
                    raise RuntimeError(
                        f"agent_control error {err.get('code')}: "
                        f"{err.get('message')}")
                return msg.get("result")

    def close(self):
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except OSError:
                    pass
                self._conn = None


def build_server(client: AgentControlClient, mcp=None):
    """Build a FastMCP server. Importable for tests with a mocked client."""
    from mcp.server.fastmcp import FastMCP

    if mcp is None:
        mcp = FastMCP(
            "qdbrowser",
            instructions=(
                "Drive a local qdbrowser session: list tabs, open new ones, "
                "navigate, click coordinates, type, scroll, snapshot the "
                "page (DOM, text, PNG), evaluate JS. Tabs must be attached "
                "via the 'attach' tool before mutating verbs (navigate, "
                "click_at, type_text, ...) will accept them."
            ),
        )

    @mcp.tool()
    def list_tabs() -> list:
        """List open browser tabs.

        Each entry: ``id`` (opaque int — pass to other tools), ``title``,
        ``url``, ``attached`` (bool), ``can_go_back``, ``can_go_forward``,
        ``loading``, ``muted``, ``pinned``, ``group``, ``profile``,
        ``zoom``, ``page_load_seq``."""
        return client.call("list_tabs")

    @mcp.tool()
    def attach(tab_id: int) -> dict:
        """Attach to a tab. Required before mutating verbs."""
        return client.call("attach", tab_id=tab_id)

    @mcp.tool()
    def detach(tab_id: int) -> dict:
        """Detach from a tab."""
        return client.call("detach", tab_id=tab_id)

    @mcp.tool()
    def open_tab(url: str | None = None, background: bool = False,
                 profile: str = "default") -> dict:
        """Open a new tab. Returns ``{id}``.

        For smoke tests or RPC plumbing checks, prefer ``about:blank``,
        a ``data:text/html`` URL, or a localhost fixture instead of a
        public web site; external pages can be slow or unavailable."""
        return client.call("open_tab", url=url, background=background,
                           profile=profile)

    @mcp.tool()
    def close_tab(tab_id: int) -> dict:
        """Close a tab."""
        return client.call("close_tab", tab_id=tab_id)

    @mcp.tool()
    def focus_tab(tab_id: int) -> dict:
        """Make a tab the current one and give it focus."""
        return client.call("focus_tab", tab_id=tab_id)

    @mcp.tool()
    def navigate(tab_id: int, url: str) -> dict:
        """Navigate a tab.

        URL with no scheme is treated as a search query. For deterministic
        automation, use ``about:blank``, ``data:text/html`` URLs, or local
        test servers when the page content itself is not under test."""
        return client.call("navigate", tab_id=tab_id, url=url)

    @mcp.tool()
    def reload(tab_id: int) -> dict:
        """Reload a tab. Returns immediately; pair with `wait_for_load`
        (or `wait_for_selector` for a specific element) if you need to
        block until the reload completes before acting."""
        return client.call("reload", tab_id=tab_id)

    @mcp.tool()
    def stop(tab_id: int) -> dict:
        """Stop loading."""
        return client.call("stop", tab_id=tab_id)

    @mcp.tool()
    def go_back(tab_id: int) -> dict:
        """Navigate back."""
        return client.call("go_back", tab_id=tab_id)

    @mcp.tool()
    def go_forward(tab_id: int) -> dict:
        """Navigate forward."""
        return client.call("go_forward", tab_id=tab_id)

    @mcp.tool()
    def get_url(tab_id: int) -> dict:
        """Get current URL/title/loading state."""
        return client.call("get_url", tab_id=tab_id)

    @mcp.tool()
    def wait_for_load(tab_id: int, timeout: float = 10.0) -> dict:
        """Block until the next full page load fires `loadFinished`.

        Use AFTER `navigate`, `reload`, `go_back`, or `go_forward` — these
        trigger a real top-level load. Do NOT use after `click_at`
        on a single-page app (Gmail, Slack, modern React/Vue sites):
        SPA clicks update the URL via History API but never fire
        `loadFinished`, so this verb will time out. For SPA clicks,
        use `wait_for_selector` to wait for the post-click DOM."""
        return client.call("wait_for_load", tab_id=tab_id, timeout=timeout)

    @mcp.tool()
    def click_at(tab_id: int, x: float, y: float,
                 button: str = "left",
                 modifiers: list | None = None) -> dict:
        """Synthesise a click at viewport CSS pixels (x, y). The click
        is JS-dispatched (`MouseEvent` on `elementFromPoint(x,y)`), so
        framework click handlers (React, Vue, Svelte) fire correctly.
        For locating coordinates: call `query_selector` first to get
        the rect, then click at its centre. After clicking, use
        `wait_for_selector` to wait for SPA state changes; `wait_for_load`
        is the wrong verb for clicks (see its description)."""
        return client.call("click_at", tab_id=tab_id, x=x, y=y,
                           button=button, modifiers=modifiers or [])

    @mcp.tool()
    def dblclick_at(tab_id: int, x: float, y: float) -> dict:
        """Double-click at viewport pixels."""
        return client.call("dblclick_at", tab_id=tab_id, x=x, y=y)

    @mcp.tool()
    def move_mouse(tab_id: int, x: float, y: float) -> dict:
        """Move mouse to viewport pixels."""
        return client.call("move_mouse", tab_id=tab_id, x=x, y=y)

    @mcp.tool()
    def scroll(tab_id: int, dx: float = 0, dy: float = 0) -> dict:
        """Scroll the page by (dx, dy) pixels."""
        return client.call("scroll", tab_id=tab_id, dx=dx, dy=dy)

    @mcp.tool()
    def type_text(tab_id: int, text: str) -> dict:
        """Type literal text into the focused element.

        Reliable sequence: call `query_selector`, click the center of
        the returned rect with `click_at`, then `type_text`. For React,
        Vue, and similar apps, assert the result with `wait_for_selector`
        or `eval_js` rather than sleeping."""
        return client.call("type_text", tab_id=tab_id, text=text)

    @mcp.tool()
    def send_keys(tab_id: int, keys: list) -> dict:
        """Send symbolic keys: enter, tab, escape, up, ctrl+l, shift+tab, ..."""
        return client.call("send_keys", tab_id=tab_id, keys=keys)

    @mcp.tool()
    def screenshot(tab_id: int, full_page: bool = False) -> dict:
        """PNG snapshot of the tab. Returns ``{width, height, png_b64}``."""
        return client.call("screenshot", tab_id=tab_id, full_page=full_page)

    @mcp.tool()
    def get_dom(tab_id: int) -> dict:
        """Outer HTML of the current document."""
        return client.call("get_dom", tab_id=tab_id)

    @mcp.tool()
    def get_visible_text(tab_id: int) -> dict:
        """innerText of body."""
        return client.call("get_visible_text", tab_id=tab_id)

    @mcp.tool()
    def eval_js(tab_id: int, script: str, timeout: float = 5.0) -> dict:
        """Run JavaScript and return the JSON-serializable result.

        Keep scripts small and self-contained. If waiting for a DOM
        change, prefer `wait_for_selector`; it polls with a deadline and
        produces a clearer success condition than an arbitrary delay."""
        return client.call("eval_js", tab_id=tab_id, script=script,
                           timeout=timeout)

    @mcp.tool()
    def query_selector(tab_id: int, selector: str) -> dict:
        """Locate a selector; returns ``{found, rect: {x,y,w,h,text}}``."""
        return client.call("query_selector", tab_id=tab_id, selector=selector)

    @mcp.tool()
    def wait_for_selector(tab_id: int, selector: str,
                          timeout: float = 5.0) -> dict:
        """Poll the DOM at 100ms intervals until `selector` matches or
        timeout. PREFER THIS over `wait_for_load` after `click_at` on
        any modern web app (SPA clicks don't trigger `loadFinished`).
        Also useful after `navigate` when you want to wait for a
        specific element rather than the whole page."""
        return client.call("wait_for_selector", tab_id=tab_id,
                           selector=selector, timeout=timeout)

    @mcp.tool()
    def pip(tab_id: int) -> dict:
        """Toggle picture-in-picture on the largest playing video in a tab."""
        return client.call("pip", tab_id=tab_id)

    @mcp.tool()
    def translate(tab_id: int, target_lang: str | None = None,
                  selection_only: bool = False) -> dict:
        """Translate the page (or current selection) via the configured
        OpenAI-compatible endpoint. Overlay shows source + translation
        side by side. Tab must be attached."""
        return client.call("translate", tab_id=tab_id,
                           target_lang=target_lang,
                           selection_only=selection_only)

    return mcp


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="qdbrowser-mcp",
        description="MCP stdio proxy for qdbrowser's agent_control socket.",
    )
    p.add_argument(
        "--socket",
        default=(os.environ.get("QDBROWSER_AGENT_SOCKET")
                 or default_socket_path()),
        help="Path to the qdbrowser agent_control Unix socket.",
    )
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    client = AgentControlClient(args.socket)
    server = build_server(client)
    try:
        server.run(transport="stdio")
    finally:
        client.close()


if __name__ == "__main__":
    main()
