# 01 — agent_control plugin drives a shell and a TUI from outside the process

**What**: with QTerminator running locally and the `agent_control` plugin
enabled, an external "fake agent" client connects to the plugin's Unix
socket, drives a plain shell command (assert via raw stream), drives a TUI
(`python3` REPL, assert via rendered screen state), opens a second tab via
the protocol, and finally detaches. Both read surfaces — `get_screen`
(rendered grid) and `tail_stream` (raw PTY bytes) — must agree.

**Why**: this is the contract test for the public plugin protocol. Every
harness path (Claude Code via MCP, opencode via MCP, anything via the
`qterminator-ctl` CLI) ultimately resolves to these JSON-RPC calls; if this
scenario passes, all transports above it are wiring problems, not protocol
problems.

## Setup

Runs on the host — no VM. Uses `QT_QPA_PLATFORM=offscreen` so it works in
CI; `term.grab()` still renders into a pixmap offscreen so screenshot
assertions remain meaningful.

```bash
export QT_QPA_PLATFORM=${QT_QPA_PLATFORM:-offscreen}
export QTERMINATOR_AGENT_CONTROL=1   # opt-in env flag the plugin honours

SOCK="${XDG_RUNTIME_DIR:-/tmp}/qterminator-agent.sock"
rm -f "$SOCK"

# Enable the plugin in user config so a clean install picks it up.
mkdir -p "$HOME/.config/qterminator"
python3 -c '
import tomllib, tomli_w, pathlib
p = pathlib.Path.home()/".config/qterminator/config.toml"
cfg = tomllib.loads(p.read_text()) if p.exists() else {}
cfg.setdefault("plugins", {})["agent_control"] = True
p.write_text(tomli_w.dumps(cfg))
'

python3 -m qterminator >/tmp/agent-control-01.log 2>&1 &
QT_PID=$!

# Wait for the socket — the plugin creates it on activate(). Bail at 6s.
for i in $(seq 1 30); do [ -S "$SOCK" ] && break; sleep 0.2; done
test -S "$SOCK" || { kill $QT_PID; exit 2; }

CLI="python3 -m qterminator.plugins._agent_test_client --sock $SOCK"
```

## Steps

### S1 — `list_tabs` returns the initial shell tab

```bash
$CLI list > /tmp/agent-control-01-s1-list.json
```

**Assert:**
- response is a JSON array of length 1
- entry has `id` (int), `title` (string), `shell_pid` > 0, `cols` >= 80,
  `rows` >= 24
- `attached` is `false`

### S2 — attach + plain command, assert via raw stream

`tail_stream` returns events `{seq, bytes_b64}` in order; the client
decodes base64 and prints the joined text.

```bash
$CLI attach 1
$CLI send 1 $'echo HELLO_FROM_AGENT\n'
sleep 0.4
$CLI tail 1 --since 0 > /tmp/agent-control-01-s2-stream.txt
```

**Assert (raw stream):**
- `s2-stream.txt` contains the literal substring `HELLO_FROM_AGENT`
  followed by a newline (the shell echoed the command back).
- `seq` numbers in the JSON event log are strictly monotonic with no
  gaps (the client errors out loudly if not — a gap means the plugin
  dropped buffered output, which breaks log-parsing harnesses).
- The titlebar of tab 1 now shows the `👁 agent` attached indicator
  (screenshot `/tmp/agent-control-01-s2.png` taken via the
  `screenshot` RPC, not desktop screenshot — the plugin's
  `screenshot` returns the QTermWidget pixmap as base64 PNG).

### S3 — drive a TUI, assert via rendered screen state

This is the screen-state path. `get_screen` returns
`{cols, rows, cursor: {x, y}, lines: [str, ...]}` — the rendered grid
with cursor position, escape sequences resolved, no SGR noise.

```bash
$CLI send 1 $'python3\n'
sleep 1.0
$CLI screen 1 > /tmp/agent-control-01-s3-screen.json
$CLI screenshot 1 /tmp/agent-control-01-s3.png
```

**Assert (screen state):**
- `lines` is an array of length `rows`; each entry is a plain string of
  width `cols` (right-padded with spaces).
- the last non-blank line starts with `>>> ` (Python 3 primary prompt).
- `cursor.y` equals the index of that prompt line; `cursor.x == 4` (the
  cursor sits in the column immediately after `>>> `).
- the screenshot at `s3.png` shows the same prompt; the visible cursor
  block aligns with the `cursor` position (eyeballed within ±1 column).

### S4 — type into the TUI, both surfaces stay consistent

```bash
$CLI send 1 $'2+2\n'
sleep 0.4
$CLI screen 1 > /tmp/agent-control-01-s4-screen.json
$CLI tail 1 --since-marker s3 > /tmp/agent-control-01-s4-stream.txt
```

**Assert:**
- `lines` contains, in order from top to bottom: a line ending `>>> 2+2`,
  a line containing exactly `4` (possibly with trailing spaces), then
  a new `>>> ` line with the cursor on it.
- `s4-stream.txt` (raw bytes since S3, SGR-stripped by the client)
  contains the substring `2+2\r\n4\r\n>>> `.
- **Cross-check**: the rendered `lines` and the SGR-stripped stream
  describe the same final visible state. If they disagree, the plugin
  is buggy — record both files in the FAIL justification.

### S5 — `open_tab` via protocol, list_tabs grows

```bash
$CLI open_tab --cwd /tmp
sleep 0.3
$CLI list > /tmp/agent-control-01-s5-list.json
```

**Assert:**
- list has length 2.
- the second entry's `working_directory` ends with `/tmp`.
- a screenshot of the QTerminator main window (host-level, via
  `import -window root` or equivalent — the plugin's `screenshot` is
  per-tab and won't show the tab strip) shows two tab-strip entries.

### S6 — detach, indicator clears, send is rejected

```bash
$CLI detach 1
$CLI send 1 $'echo NOPE\n' > /tmp/agent-control-01-s6-resp.json || true
```

**Assert:**
- `s6-resp.json` is a JSON-RPC error with `code: -32001` and
  `message` matching `/not attached/i` (no `NOPE` reaches the PTY).
- the `👁 agent` indicator on tab 1's titlebar is gone (screenshot
  via per-tab `screenshot` RPC after a fresh `attach` — yes, that
  re-attaches, document this if you take the screenshot last).

## Teardown

```bash
kill $QT_PID 2>/dev/null
wait $QT_PID 2>/dev/null
rm -f "$SOCK" /tmp/agent-control-01-*.{json,txt,png,log}
```

Restore the original config if the runner mutated it (Setup writes
`agent_control = true`; if the user had it `false`, flip it back).

## Notes for the runner

- **Two read surfaces, two failure modes.** A TUI assertion that only
  reads the raw stream will pass on `\x1b[H\x1b[2J>>> ` even if the
  screen never actually rendered the prompt because the widget
  crashed mid-redraw. Assert on `get_screen` for TUIs. Conversely, a
  log-parsing assertion that only reads the screen grid will miss
  output that scrolled past the visible window. Use `tail_stream`
  there.
- **`offscreen` doesn't simulate a real compositor.** All input —
  including arrows, function keys, Ctrl-chars — is sent via
  `QTermWidget.sendText()` with the appropriate ANSI escape sequence
  (`\x1b[A` for up-arrow, `\x03` for Ctrl-C, etc.). The binding does
  not expose `sendKeyEvent`, so `QKeyEvent` synthesis is not an option
  and not needed: `sendText` writes straight to the PTY, which is
  what TUIs read from anyway. A separate VM-based scenario (later)
  covers the compositor input path; this host-level scenario is the
  protocol contract.
- **Screen-state implementation.** The QTermWidget Python binding
  exposes terminal geometry (`screenColumnsCount`, `screenLinesCount`,
  `historyLinesCount`) but no cursor position or screen-content accessor
  (no `cursorRow/Col`, no per-cell read). The plugin therefore maintains
  its own shadow screen by feeding `receivedData` into a `pyte.Screen`
  and reads `screen.display` / `screen.cursor` for the RPC response. The raw
  stream surface comes from the same `receivedData` signal, before
  pyte processes it. If pyte is unavailable at import time the plugin
  must fail to load (not silently degrade) so `get_screen` never
  lies; the scenario's setup will detect that via the socket never
  appearing.
- **`_agent_test_client`** lives under `qterminator/plugins/` with a
  leading underscore so the plugin discovery loop ignores it
  (`plugin.py` skips `_*.py`). It's a thin JSON-RPC client used only
  by tests, not a runtime dependency.
- If the socket never appears within 6s, `cat /tmp/agent-control-01.log` —
  the most likely culprits are: `QTERMINATOR_AGENT_CONTROL` unset,
  plugin not enabled in config, or another QTerminator process already
  holding the socket path (clean stale `$SOCK` and stale `qterminator`
  processes in Setup).
