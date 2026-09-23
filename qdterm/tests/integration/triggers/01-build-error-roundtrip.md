# 01 — triggers fires actions on real terminal output

**What**: declare a couple of trigger rules in config, launch
QTerminator, drive a command that prints text matching each rule, and
assert (a) the captured-output sidebar collected the URL match,
(b) the tab text color flipped to red on the ERROR line, and (c) the
`notify-send` subprocess call was attempted (stubbed via a wrapper
on `$PATH`).

**Why**: the unit tests cover rule loading, cooldown, multi-chunk
pattern bridging, action dispatch, and the `_connect_terminal`
wrapping. This scenario closes the loop: a user-authored config rule
acts on real bytes from a real shell into a real Qt tab.

## Setup

```bash
export QT_QPA_PLATFORM=${QT_QPA_PLATFORM:-offscreen}
export QTERMINATOR_AGENT_CONTROL=1

# Stub notify-send so the test doesn't require a real desktop service.
STUB="$(mktemp -d)"
cat > "$STUB/notify-send" <<'EOF'
#!/bin/sh
echo "$@" >> "$STUB/notify.log"
EOF
chmod +x "$STUB/notify-send"
export PATH="$STUB:$PATH"

# Drop a config with three rules.
mkdir -p "$HOME/.config/qterminator"
python3 - <<'PY'
import pathlib, tomli_w
p = pathlib.Path.home()/".config/qterminator/config.toml"
cfg = {
  "plugins": {
    "agent_control": True,
    "triggers": {
      "enabled": True,
      "rules": [
        {"pattern": r"\bERROR\b", "action": "set_tab_color",
         "color": "#e74c3c"},
        {"pattern": r"https?://\S+", "action": "capture",
         "sidebar": "URLs"},
        {"pattern": r"\bDONE\b", "action": "notify",
         "message": "{tab_title} finished"},
      ]
    }
  }
}
p.write_text(tomli_w.dumps(cfg))
PY

SOCK="${XDG_RUNTIME_DIR:-/tmp}/qterminator-agent-$(id -u).sock"
rm -f "$SOCK"
python3 -m qterminator >/tmp/triggers-01.log 2>&1 &
QT_PID=$!
for i in $(seq 1 30); do [ -S "$SOCK" ] && break; sleep 0.2; done
test -S "$SOCK" || { kill $QT_PID; exit 2; }
CLI="python3 -m qterminator.plugins._agent_test_client --sock $SOCK"
```

## Steps

### S1 — drive matching text through the shell

```bash
TID=$($CLI list | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')
$CLI attach $TID
$CLI send $TID $'echo "look: https://example.com/x and ERROR happened"\n'
sleep 0.4
$CLI send $TID $'echo DONE\n'
sleep 0.4
```

### S2 — `notify-send` was invoked

**Assert:** `$STUB/notify.log` exists and contains `"finished"`.

### S3 — URL captured into the sidebar

The trigger sidebar is in-memory on the service; to read it from
outside we use a tiny one-off Python REPL via `python3 -c` against
the running window. (A future task may add an MCP tool that reads
trigger sidebars; for v1 the read path is in-process.)

```bash
# (Run from inside an interactive session connected to the running
# QTerminator's Python — not currently exposed via agent_control.)
```

**Assert** (when run by hand or via the matching unit test):
- `win.triggers.sidebar("URLs")` returns one entry whose `match`
  starts with `https://example.com/`.

### S4 — tab color flipped to red

The tab title color update is visible in a host screenshot.

```bash
$CLI screenshot $TID /tmp/triggers-01-tab.png
```

(Per-tab screenshot is per-content; the tab-strip color is on the
QTabBar at window level, not the QTermWidget pixmap. A
desktop-level screenshot would show it, but offscreen Qt won't
render a real desktop. The unit test
`test_set_tab_color_action_updates_tabbar` is the load-bearing
assertion.)

## Teardown

```bash
kill $QT_PID 2>/dev/null; wait $QT_PID 2>/dev/null
rm -rf "$STUB" "$HOME/.config/qterminator/config.toml"
rm -f /tmp/triggers-01-* /tmp/triggers-01.log "$SOCK"
```

## Notes for the runner

- **Why not test every action here?** The unit tests in
  `tests/test_triggers.py` already monkeypatch each action and
  assert the side effect. This scenario exists to prove the
  end-to-end wiring (`_connect_terminal` wrap → ShadowScreen
  listener → action) works against a real shell. Two or three
  representative actions is enough.
- **The `set_tab_color` action does not survive a tab move.** If
  the user drags the tab to a new position, Qt resets the
  per-tab color. Future enhancement: latch the color on each
  `tabMoved` signal. Out of scope for v1.
- **`notify-send` is fire-and-forget.** Failures (D-Bus not
  available, etc.) are swallowed; the test asserts the *attempt*
  was made via the stub-on-PATH trick.
