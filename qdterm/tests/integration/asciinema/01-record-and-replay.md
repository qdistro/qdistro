# 01 — agent records its own session, replay reproduces the visible grid

**What**: an agent (via the agent_control / qterminator-mcp surface)
opens a tab, attaches, starts a recording, runs a short scripted
interaction including a TUI (Python REPL), stops the recording. A
second QTerminator instance (or `asciinema play`) then replays the
cast and the rendered grid is asserted to match the live grid taken
at the moment recording stopped.

**Why**: recordings are useful only if they're faithful. This is the
"cast is replayable" contract test, end-to-end. It also exercises the
MCP tool surface — start_recording / stop_recording — in the same
loop, so a regression in either layer fails this scenario.

## Setup

Host-only; offscreen Qt. Uses the `_agent_test_client` and the
`asciinema` CLI for replay validation.

```bash
export QT_QPA_PLATFORM=${QT_QPA_PLATFORM:-offscreen}
export QTERMINATOR_AGENT_CONTROL=1

CAST_DIR=$(mktemp -d)
CAST_PATH="$CAST_DIR/agent-recording.cast"

CONF_DIR=$(mktemp -d)
mkdir -p "$CONF_DIR"
python3 - <<EOF
import tomli_w, pathlib
p = pathlib.Path("$CONF_DIR")/"config.toml"
p.write_text(tomli_w.dumps({
    "plugins": {
        "asciinema_record": {"enabled": True, "save_dir": "$CAST_DIR"},
    }
}))
EOF
# Point the qterminator config dir at our temp.
export QTERMINATOR_CONFIG_DIR="$CONF_DIR"

SOCK="${XDG_RUNTIME_DIR:-/tmp}/qterminator-agent-$(id -u).sock"
rm -f "$SOCK"

python3 -m qterminator >/tmp/asciinema-01.log 2>&1 &
QT_PID=$!
for i in $(seq 1 30); do [ -S "$SOCK" ] && break; sleep 0.2; done
test -S "$SOCK" || { kill $QT_PID; exit 2; }

CLI="python3 -m qterminator.plugins._agent_test_client --sock $SOCK"
```

## Steps

### S1 — agent attaches and starts a recording

```bash
TAB_ID=$($CLI list | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')
$CLI attach $TAB_ID

# Start recording via raw JSON-RPC since the test client wrapper
# doesn't have a dedicated subcommand for it; this mirrors what the
# MCP tool start_recording does under the hood.
python3 - <<EOF
import socket, json
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect("$SOCK")
s.sendall((json.dumps({
    "jsonrpc":"2.0","id":1,"method":"start_recording",
    "params":{"tab_id":$TAB_ID,"path":"$CAST_PATH"}
})+"\n").encode())
print(s.recv(4096).decode())
EOF
sleep 0.3
```

**Assert:**
- `list_tabs[0].recording` is `true`.
- `list_tabs[0].recording_path` equals `$CAST_PATH`.
- The cast file exists and starts with a JSON header containing
  `"version": 3` and a `term` object with positive `cols`/`rows`.

### S2 — agent drives a TUI (python REPL) inside the recorded session

```bash
$CLI send $TAB_ID $'python3\n'
sleep 0.8
$CLI send $TAB_ID $'print("HELLO_RECORDING")\n'
sleep 0.4
$CLI send $TAB_ID $'2+2\n'
sleep 0.4
$CLI screen $TAB_ID > /tmp/asciinema-01-live.json
```

**Assert:**
- The live `get_screen` snapshot's `lines` array contains the
  literal `HELLO_RECORDING` somewhere, and a line containing exactly
  `4` (the result of `2+2`).

### S3 — agent stops the recording

```bash
python3 - <<EOF
import socket, json
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect("$SOCK")
s.sendall((json.dumps({
    "jsonrpc":"2.0","id":1,"method":"stop_recording",
    "params":{"tab_id":$TAB_ID}
})+"\n").encode())
print(s.recv(4096).decode())
EOF
```

**Assert:**
- The stop response contains `bytes_written > 0`, `event_count >= 4`
  (header + at least the python prompt + HELLO_RECORDING + 4 +
  trailing prompt), and `duration > 0`.
- After stop, `list_tabs[0].recording` is `false`.

### S4 — replay the cast and assert the final grid matches S2

The "agent-assisted replay" assertion. Two acceptable execution
paths; runner picks whichever it can perform:

**Path A: `asciinema play` into a fresh qterminator tab**

```bash
$CLI open_tab
sleep 0.5
NEW_ID=$($CLI list | python3 -c 'import json,sys; print(json.load(sys.stdin)[-1]["id"])')
$CLI attach $NEW_ID
$CLI send $NEW_ID $"asciinema play --speed 999 $CAST_PATH\n"
sleep 2.0
$CLI screen $NEW_ID > /tmp/asciinema-01-replay-tab.json
```

Compare `live.json` (from S2) against `replay-tab.json`. Strip
trailing whitespace from each line. The two grids may differ by the
literal `asciinema play` command line at the top, plus any chrome
asciinema adds — but the substrings `HELLO_RECORDING` and `4` must
appear in `replay-tab.json` and in the same relative order.

**Path B: offline pyte replay** (no asciinema CLI needed)

```bash
python3 - <<EOF > /tmp/asciinema-01-replay-pyte.json
import json, pyte, sys
with open("$CAST_PATH") as f:
    lines = [l for l in f if l.strip()]
hdr = json.loads(lines[0])
screen = pyte.Screen(hdr["term"]["cols"], hdr["term"]["rows"])
stream = pyte.Stream(screen)
for ev in (json.loads(l) for l in lines[1:]):
    if ev[1] == "o":
        stream.feed(ev[2])
json.dump({
    "cols": screen.columns,
    "rows": screen.lines,
    "cursor": {"x": screen.cursor.x, "y": screen.cursor.y},
    "lines": list(screen.display),
}, sys.stdout)
EOF
```

Compare `live.json` and `replay-pyte.json`:
- `[l.rstrip() for l in live.lines]` and
  `[l.rstrip() for l in replay-pyte.lines]` should be identical
  modulo trailing blank rows that one snapshot might include and
  the other not (compare line-by-line after stripping trailing
  empties from each).

**Path B is the canonical correctness test** and should always be
run. Path A is the user-facing UX check; it's nice when available
but not required for PASS.

**Assert (S4):**
- The strings `HELLO_RECORDING` and the standalone `4` appear in
  the replayed grid in the correct order.
- For Path B: stripped, non-blank lines of `live.lines` are a
  subsequence of stripped, non-blank lines of `replay-pyte.lines`
  (subsequence rather than equality because pyte may have evicted
  early scrollback that the live ShadowScreen still had).

### S5 — crash-safety: kill QTerminator mid-record, recording still parseable

```bash
$CLI attach $TAB_ID
python3 - <<EOF
import socket, json
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect("$SOCK")
s.sendall((json.dumps({
    "jsonrpc":"2.0","id":1,"method":"start_recording",
    "params":{"tab_id":$TAB_ID,"path":"$CAST_DIR/crash.cast"}
})+"\n").encode())
s.recv(4096)
EOF
$CLI send $TAB_ID $'echo CRASH_MARKER\n'
sleep 0.4
kill -9 $QT_PID
```

**Assert:**
- `$CAST_DIR/crash.cast` exists, has a header line, and every
  subsequent line is valid JSON with shape `[number, "o"|"i"|"r"|"x"|"m", string]`.
- Header has `"version": 3`.
- At least one event's `data` substring contains `CRASH_MARKER`.

(This proves the line-flushed, append-mode writing is crash-safe.)

## Teardown

```bash
kill $QT_PID 2>/dev/null
wait $QT_PID 2>/dev/null
rm -rf "$CAST_DIR" "$CONF_DIR"
rm -f "$SOCK" /tmp/asciinema-01-*.{json,log}
```

## Notes for the runner

- **The "agent" here is the test driver.** A live LLM is not needed.
  This scenario exists to be runnable in CI by any vision-blind
  process — Path B is plain `pyte` arithmetic.
- **Path A's value** is catching escape-sequence drift between our
  ShadowScreen and the cast file. If Path B passes but A fails, the
  bug is in how `asciinema play` parses our cast (most likely an
  invalid escape that pyte tolerates but xterm doesn't). If A
  passes but B fails, something's wrong in our pyte path —
  unlikely since both ends use pyte.
- **No `restore_on_start` interaction here.** This scenario is about
  the recorder. The tmux-mode scenario covers persistence; we
  intentionally do not couple them so a failure tells us which
  plugin regressed.
- **TUI-specific:** Python REPL is chosen because its prompt is
  stable (`>>> `) and arithmetic output (`4`) is unambiguous.
  Don't substitute `vim` or `htop` — their redraws stress
  resize-event reconstruction, which is a separate scenario
  (`02-resize-during-record.md`, TODO).
