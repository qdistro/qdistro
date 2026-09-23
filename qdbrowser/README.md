# qdbrowser

A Qt-based web browser, sibling of [qterminator](../qterminator) and [qdshell](../qdshell).

PyQt6 + QtWebEngine, plugin-driven, agent-controllable. Targets Vivaldi-class feature density without the Chrome lock-in.

## Role in qdistro

qdbrowser is the first-party browser for qdistro's "one owner, many silos,
dynamic sessions" model. It is intended to be the browser surface that can
participate directly in qdistro policy: tab/window identity, password-vault
approval, page extraction, screenshots, automation, and future cross-silo handoff
all route through trusted bridge APIs rather than page-controlled DOM alone.

Use qdbrowser when the browser should be part of the qdistro control plane. Use
[qdchrome-extension](../qdchrome-extension) or
[qdfirefox-extension](../qdfirefox-extension) when compatibility with upstream
Chromium/Firefox is the primary requirement.

## Status

v0.1.0 — early. Core browsing, tabs, recursive splits, side panel, command palette, sessions, content blocker, agent control via MCP.

## Why "qd"?

Part of the qdistro stack:
- `qdwin` — C compositor (Wayland)
- `qdshell` — QML shell
- `qterminator` — Qt terminal emulator
- `qdbrowser` — *this*

Shared philosophy: PyQt6 where possible, filesystem-discovered plugins, TOML config, agent control via Unix-socket JSON-RPC wrapped in MCP, pytest + libvirt-VM testing.

## Install

```bash
# System deps (openSUSE / Ubuntu / Fedora auto-detected):
just install-deps

# Optional: MCP support for agent-driving:
just install-mcp

# Install qdbrowser itself in editable mode:
just install

# Run:
just run
# or with the agent_control socket exposed:
just run-agent
```

## Features (v0)

**Browsing & navigation**
- Multi-tab with tab stacks (groups)
- Recursive split panes per tab (drag a tab into a split, or `Ctrl+Shift+O` / `Ctrl+Shift+E`)
- URL bar with smart suggestions (history + bookmarks)
- Back / forward / reload / home
- Find-in-page

**Power user**
- Quick command palette (`Ctrl+E`) — fuzzy across tabs, bookmarks, history, settings
- Mouse gestures
- Configurable keymap (TOML)
- Sessions + named workspaces
- Reader mode
- Per-tab zoom, mute, pin
- Page screenshots (full / visible / element)
- Note-taking panel

**Privacy / blocking**
- Built-in tracker + ad blocker (hosts-list URL interceptor)
- Per-site permissions
- HTTPS-only mode toggle

**Side panel** (Vivaldi-style)
- Bookmarks
- History
- Downloads
- Notes
- Web panels (sites pinned to side panel)

**Agent control**
- Unix-socket JSON-RPC (mirror of qterminator/agent_control)
- `qdbrowser-mcp` stdio server for Claude Code / opencode
- Verbs: `list_tabs`, `open_tab`, `close_tab`, `navigate`, `screenshot`, `click_at`, `type_text`, `send_keys`, `scroll`, `get_dom`, `get_visible_text`, `eval_js`, `wait_for_load`, `wait_for_selector`

## Architecture

```
qdbrowser/
├── qdbrowser/
│   ├── __main__.py        # CLI + QApplication bootstrap
│   ├── window.py          # MainWindow: tabs, toolbar, side panel
│   ├── webview.py         # QWebEngineView wrapper (signals, title, icon)
│   ├── splitter.py        # Recursive SplitContainer (mirrors qterminator)
│   ├── layout.py          # Session serialize / restore
│   ├── config.py          # TOML singleton
│   ├── plugin.py          # Plugin base classes + manager
│   ├── theme.py           # Dark / light palette + stylesheet
│   ├── side_panel.py      # Dockable side panel host
│   ├── mcp_server.py      # FastMCP stdio proxy → agent socket
│   └── plugins/
│       ├── agent_control.py        # JSON-RPC Unix socket
│       ├── bookmarks.py
│       ├── history.py
│       ├── downloads.py
│       ├── command_palette.py      # Ctrl+E launcher
│       ├── content_blocker.py      # Hosts-list URL interceptor
│       ├── reader_mode.py
│       ├── notes.py
│       ├── sessions.py
│       ├── workspaces.py
│       ├── mouse_gestures.py
│       ├── page_actions.py         # zoom / mute / pin
│       ├── screenshot.py
│       ├── web_panels.py
│       └── tab_stacks.py
├── tests/
│   ├── conftest.py
│   ├── test_*.py
│   └── integration/
│       └── vm/                     # libvirt + bats GUI scenarios
└── justfile
```

## Agent control (MCP)

Add to your `mcp.json`:

```json
{
  "mcpServers": {
    "qdbrowser": { "command": "qdbrowser-mcp" }
  }
}
```

Launch qdbrowser with `just run-agent` (sets `QDBROWSER_AGENT_CONTROL=1`). The agent socket lives at `$XDG_RUNTIME_DIR/qdbrowser-agent-$UID.sock`, secured by `SO_PEERCRED` (same UID only).

## Testing

Unit + headless GUI tests:
```bash
just test
```

VM-based integration tests (requires libvirt template `QDWIN_VM_TEMPLATE`, mirrors qdistro pattern):
```bash
just test-vm
```

See [AGENTS.md](AGENTS.md) for the agent test harness.
