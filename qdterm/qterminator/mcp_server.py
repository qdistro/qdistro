"""MCP transport wrapper for the agent_control plugin.

Exposes QTerminator's agent_control RPC surface as an MCP stdio
server so harnesses that speak MCP (Claude Code, opencode, etc.) can
drive tabs with a one-line ``mcp.json`` entry instead of a custom
adapter:

    {
      "mcpServers": {
        "qterminator": { "command": "qterminator-mcp" }
      }
    }

The server is a thin proxy. Each MCP tool call maps 1:1 to an
agent_control JSON-RPC call over the local Unix socket. The proxy
holds a single persistent connection to agent_control for its
lifetime — so attach state survives across MCP tool calls (you can
``attach`` once and then ``send_text`` / ``get_screen`` many times in
follow-up turns).

Socket path resolution:

  1. ``--socket /path`` command-line flag.
  2. ``$QTERMINATOR_AGENT_SOCKET`` env var.
  3. ``$XDG_RUNTIME_DIR/qterminator-agent-$UID.sock`` (the default
     agent_control opens).

If agent_control is not running (no socket), the server still starts
and registers tools — calls fail at invocation time with a clear
error rather than refusing to start. That matches how Claude Code
expects MCP servers to behave (start fast, fail noisily).
"""

import argparse
import json
import os
import socket
import threading
from typing import Any

from mcp.server.fastmcp import FastMCP


def default_socket_path() -> str:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(runtime_dir, f"qterminator-agent-{os.getuid()}.sock")


class AgentControlClient:
    """Persistent JSON-RPC client over a Unix socket. Thread-safe."""

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

    def _read_line(self) -> bytes:
        while b"\n" not in self._buf:
            chunk = self._conn.recv(65536)
            if not chunk:
                raise RuntimeError(
                    "agent_control connection closed unexpectedly"
                )
            self._buf += chunk
        line, _, rest = self._buf.partition(b"\n")
        self._buf = rest
        return line

    def call(self, method: str, **params) -> Any:
        """Send a JSON-RPC request and return the ``result`` field.
        Raises ``RuntimeError`` on protocol errors and includes the
        full RPC error body for visibility upstream."""
        with self._lock:
            self._ensure()
            rid = self._next_id
            self._next_id += 1
            req = {
                "jsonrpc": "2.0", "id": rid,
                "method": method, "params": params,
            }
            try:
                self._conn.sendall((json.dumps(req) + "\n").encode("utf-8"))
            except OSError:
                # Connection died — try once to reconnect.
                self._conn = None
                self._ensure()
                self._conn.sendall((json.dumps(req) + "\n").encode("utf-8"))
            while True:
                line = self._read_line()
                if not line.strip():
                    continue
                msg = json.loads(line.decode("utf-8"))
                # Skip events (server-initiated, no id).
                if msg.get("id") != rid:
                    continue
                if "error" in msg:
                    err = msg["error"]
                    raise RuntimeError(
                        f"agent_control error {err.get('code')}: "
                        f"{err.get('message')}"
                    )
                return msg.get("result")

    def close(self):
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except OSError:
                    pass
                self._conn = None


def build_server(client: AgentControlClient,
                 mcp: FastMCP | None = None) -> FastMCP:
    """Construct (or extend) a FastMCP instance with the QTerminator
    tools. Exposed as a function so tests can build a server with a
    mocked client and a private FastMCP instance."""
    if mcp is None:
        mcp = FastMCP(
            "qterminator",
            instructions=(
                "Drive a local QTerminator session: list tabs, attach to "
                "one, send text or keystrokes, snapshot the rendered "
                "screen (TUIs) or read the raw PTY stream (logs), "
                "screenshot a tab, open new tabs. Tabs must be attached "
                "via the 'attach' tool before send_text / send_keys / "
                "get_screen / tail_stream will work."
            ),
        )

    @mcp.tool()
    def list_tabs() -> list[dict]:
        """List open terminal tabs.

        Each entry: ``id`` (int — opaque, pass to other tools),
        ``title``, ``shell_pid``, ``cols``, ``rows``, ``attached``
        (bool), ``working_directory``, ``tmux_session`` (str | None),
        ``shared_via_mosh`` (list[int] of UDP ports), ``cwd_reported``
        (str | None, from OSC 7 if the shell emits it),
        ``last_command`` ({text?, exit_status, started_at, finished_at,
        cwd} | None, populated when shell_integration is loaded)."""
        return client.call("list_tabs")

    @mcp.tool()
    def attach(tab_id: int) -> dict:
        """Attach to a tab. Required before send_text / send_keys /
        get_screen / tail_stream. Attach is per-MCP-server-process and
        persists across tool calls."""
        return client.call("attach", tab_id=tab_id)

    @mcp.tool()
    def detach(tab_id: int) -> dict:
        """Detach from a tab. Releases the shadow screen if no other
        consumer holds it."""
        return client.call("detach", tab_id=tab_id)

    @mcp.tool()
    def send_text(tab_id: int, text: str) -> dict:
        """Type ``text`` into a tab. Pass ANSI escape sequences
        directly for special keys (e.g. ``'\\x1b[A'`` for up arrow,
        ``'\\x03'`` for Ctrl-C). For symbolic keys use ``send_keys``
        instead."""
        return client.call("send_text", tab_id=tab_id, text=text)

    @mcp.tool()
    def send_keys(tab_id: int, keys: list[str]) -> dict:
        """Send symbolic key names. Supported: single characters,
        ``enter``, ``tab``, ``backspace``, ``escape``, ``space``,
        arrows (``up``/``down``/``left``/``right``), ``home``/``end``,
        ``pageup``/``pagedown``, ``insert``/``delete``, ``f1``-``f12``,
        and ``ctrl+<letter>``."""
        return client.call("send_keys", tab_id=tab_id, keys=keys)

    @mcp.tool()
    def get_screen(tab_id: int) -> dict:
        """Snapshot the rendered terminal grid + cursor. Returns
        ``cols``, ``rows``, ``cursor`` (``{x, y}``), and ``lines``
        (list of strings, one per visible row). Best for TUI
        introspection — escape sequences are resolved."""
        return client.call("get_screen", tab_id=tab_id)

    @mcp.tool()
    def tail_stream(tab_id: int, since: int = 0) -> dict:
        """Return raw PTY bytes for a tab since sequence number
        ``since`` (0 = from the start of buffered history, up to ~1
        MiB). Returns ``{latest_seq, bytes_b64}``. Best for log
        parsing where exact bytes matter."""
        return client.call("tail_stream", tab_id=tab_id, since=since)

    @mcp.tool()
    def screenshot(tab_id: int) -> dict:
        """PNG snapshot of a tab's terminal widget. Returns
        ``{width, height, png_b64}``."""
        return client.call("screenshot", tab_id=tab_id)

    @mcp.tool()
    def open_tab(working_directory: str | None = None) -> dict:
        """Open a new tab. Returns ``{id}`` of the new tab. If
        QTerminator has tmux_mode enabled, the new tab will be
        tmux-backed automatically."""
        return client.call("open_tab", working_directory=working_directory)

    @mcp.tool()
    def close_tab(tab_id: int) -> dict:
        """Close a tab. The shell process is signalled to exit."""
        return client.call("close_tab", tab_id=tab_id)

    @mcp.tool()
    def start_recording(tab_id: int,
                        path: str | None = None,
                        capture_input: bool = False) -> dict:
        """Begin recording a tab to an asciicast v3 ``.cast`` file.

        Default save dir is ``~/Videos/qterminator/``; pass ``path``
        to override. ``capture_input`` records keystrokes too (off by
        default — recording input captures passwords). Tab must be
        attached. Returns ``{path, started_at, cols, rows,
        capture_input}``.
        """
        return client.call(
            "start_recording", tab_id=tab_id, path=path,
            capture_input=capture_input,
        )

    @mcp.tool()
    def command_history(tab_id: int, limit: int = 50) -> dict:
        """Recent completed shell commands for a tab.

        Requires the ``shell_integration`` plugin and a shell that emits
        OSC 133 sequences (oh-my-zsh, starship, bash-preexec, fish 3.6+,
        kitty/iTerm2/vscode shell-integration). Returns
        ``{records: [...], cwd_reported}`` where each record has
        ``text`` (null if capture-command-text is off, the default),
        ``exit_status``, ``started_at``, ``finished_at``, ``cwd``, and
        ``output_seq_range``. When the ``command_telemetry`` plugin is
        also loaded, records additionally carry a ``telemetry`` field
        with ``duration``, ``cpu_seconds``, ``peak_rss_bytes`` and
        ``process_count``."""
        return client.call("command_history", tab_id=tab_id, limit=limit)

    @mcp.tool()
    def stop_recording(tab_id: int,
                       exit_status: int | None = None) -> dict:
        """Stop the active recording on a tab. Returns ``{path,
        bytes_written, event_count, duration}``. The cast file is
        flushed and closed; ``asciinema play <path>`` will replay it.
        Tab must be attached."""
        return client.call(
            "stop_recording", tab_id=tab_id, exit_status=exit_status,
        )

    return mcp


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="qterminator-mcp",
        description="MCP stdio server proxying to QTerminator's agent_control "
                    "Unix socket.",
    )
    p.add_argument(
        "--socket",
        default=os.environ.get("QTERMINATOR_AGENT_SOCKET")
                or default_socket_path(),
        help="Path to agent_control's Unix socket (default: "
             "$XDG_RUNTIME_DIR/qterminator-agent-$UID.sock)",
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
