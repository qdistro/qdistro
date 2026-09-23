# 01 — tmux_mode tabs are visually indistinguishable from plain shell tabs

**What**: with `[plugins.tmux_mode] enabled = true` in the user config,
spawn a new QTerminator tab and confirm there is *no* visible giveaway
that tmux is the underlying program — no green status bar, no "exec
tmux" trace in the scrollback, no "[detached]"-style stray text. Then
verify the agent_control plugin reports the tab as tmux-backed, kill
QTerminator, relaunch it, and confirm the same session reattaches into
a tab.

**Why**: the value proposition of "born in tmux" is exactly the *user
doesn't notice*. A pixel- or scrollback-level regression that exposes
the tmux client (status bar, "exec tmux..." line, escape-time lag)
breaks the contract; OCR-on-screenshot is the only way to catch it.

## Setup

Runs on the host. Driven by the same fake-agent client used by the
agent_control scenario. Requires `tmux` installed.

```bash
export QT_QPA_PLATFORM=${QT_QPA_PLATFORM:-offscreen}
export QTERMINATOR_AGENT_CONTROL=1

# Fresh tmux server on a private socket so we don't disturb the user's
# real tmux sessions.
export TMUX_TMPDIR=$(mktemp -d)
trap 'tmux -S "$TMUX_TMPDIR/default" kill-server 2>/dev/null; rm -rf "$TMUX_TMPDIR"' EXIT

# Enable plugin in user config.
CONF_DIR=$(mktemp -d)
mkdir -p "$CONF_DIR"
python3 - <<EOF
import tomli_w, pathlib
p = pathlib.Path("$CONF_DIR")/"config.toml"
p.write_text(tomli_w.dumps({
    "plugins": {
        "tmux_mode": {
            "enabled": True,
            "session_prefix": "qterm",
            "restore_on_start": True,
        }
    }
}))
EOF
export XDG_CONFIG_HOME="$CONF_DIR/.."  # Config reads CONFIG_DIR from qterminator.config — point CONFIG_DIR there
# (Override CONFIG_DIR through env in the launcher script of choice.)

SOCK="${XDG_RUNTIME_DIR:-/tmp}/qterminator-agent-$(id -u).sock"
rm -f "$SOCK"

python3 -m qterminator >/tmp/tmux-mode-01.log 2>&1 &
QT_PID=$!
for i in $(seq 1 30); do [ -S "$SOCK" ] && break; sleep 0.2; done
test -S "$SOCK" || { kill $QT_PID; exit 2; }

CLI="python3 -m qterminator.plugins._agent_test_client --sock $SOCK"
```

## Steps

### S1 — initial tab is tmux-backed; no visible tmux chrome

```bash
sleep 1.5  # let tmux session start and pyte settle
$CLI list > /tmp/tmux-mode-01-s1.json
$CLI attach $($CLI list | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')
$CLI screenshot $TAB_ID /tmp/tmux-mode-01-s1.png  # via plugin
```

**Assert (no visible tmux chrome):**
- `list[0].tmux_session` is a non-null string starting with `qterm-`.
- `list[0].title` does **not** contain the literal text `tmux` (tmux's
  default title-rewrite would expose it; our config disables it).
- OCR of the screenshot must **not** find any of these strings: `tmux`,
  `[detached]`, `[exited]`, `bash $`-style prompt embedded inside a
  visible status bar at the bottom row.
- The bottom row of `get_screen.lines` is **not** an inverted-color
  status line — assert by checking the last row's text is either
  empty (all spaces) or contains the shell prompt characters, *not*
  a session-name indicator like `[0] 0:bash*`.
- The cursor is on the shell prompt — `get_screen.cursor` y equals
  the index of the line whose tail contains the prompt sigil (`$ ` or
  `# ` or `> `). Concretely: scan `lines` from bottom up for the
  first non-empty line; that line is the cursor's line.

### S2 — running a command produces output indistinguishable from a non-tmux tab

```bash
$CLI send $TAB_ID $'echo TMUX_INVISIBLE_TEST_$$\n'
sleep 0.4
$CLI tail $TAB_ID --since 0 > /tmp/tmux-mode-01-s2-stream.txt
$CLI screen $TAB_ID > /tmp/tmux-mode-01-s2-screen.json
```

**Assert:**
- Raw stream contains the literal `TMUX_INVISIBLE_TEST_<pid>` followed
  by a shell-prompt sigil on the next line (tmux did not insert
  status-line redraw bytes between echo and prompt).
- Screen state's `lines` has the marker visible, no tmux status line at
  the bottom, cursor on the line after the marker.

### S3 — closing the tab keeps the tmux session alive

```bash
# Record the session name from S1.
SESS=$(python3 -c 'import json; print(json.load(open("/tmp/tmux-mode-01-s1.json"))[0]["tmux_session"])')
$CLI close $TAB_ID
sleep 0.5
tmux -S "$TMUX_TMPDIR/default" list-sessions -F '#{session_name}' > /tmp/tmux-mode-01-s3.txt || true
```

**Assert:**
- `tmux ls` output contains `$SESS` (closing the tab killed the client,
  not the server-held session).

### S4 — relaunching QTerminator reattaches the same session into a new tab

```bash
kill $QT_PID
wait $QT_PID 2>/dev/null
rm -f "$SOCK"

python3 -m qterminator >>/tmp/tmux-mode-01.log 2>&1 &
QT_PID=$!
for i in $(seq 1 30); do [ -S "$SOCK" ] && break; sleep 0.2; done
sleep 1.5  # let restore_on_start fire (150ms debounce + tmux attach)
$CLI list > /tmp/tmux-mode-01-s4.json
```

**Assert:**
- `list` has at least one entry whose `tmux_session == $SESS`.
- A screenshot of that tab shows the same shell prompt the previous
  tab had (any history is preserved by tmux's history-limit; the
  prompt line is visually identical to S1's bottom-row prompt).

## Teardown

```bash
kill $QT_PID 2>/dev/null
wait $QT_PID 2>/dev/null
tmux -S "$TMUX_TMPDIR/default" kill-server 2>/dev/null || true
rm -rf "$TMUX_TMPDIR" "$CONF_DIR"
rm -f "$SOCK" /tmp/tmux-mode-01-*.{json,txt,png,log}
```

## Notes for the runner

- **The visual-equivalence assertions are the heart of this scenario.**
  If a future tmux release defaults `status` back to "on" or starts
  emitting a startup banner, this scenario should FAIL loudly with the
  screenshot + OCR output in the justification — don't massage the
  scenario to accept the regression. Update the plugin's tmux config
  instead.
- **`tmux_session` on the *initial* tab.** The plugin restores only
  *existing* sessions on activate. On a fresh install with no
  `qterm-*` sessions running, the first tab created via the normal
  init path (in `MainWindow.__init__`) gets its shell via the
  `_shell_provider` hook — that's tmux. The scenario tests both: the
  initial-tab path (S1) and the restore path (S4).
- **Why `restore_on_start = true`.** Without it, S4 relaunches but
  doesn't auto-attach; the orphan session would still exist but no
  tab would point to it. We test restoration explicitly because it's
  the only way the user's "I closed by accident" workflow is
  recoverable without manual `tmux attach`.
- **Don't conflate this with `tmux_integration.py`**. That older plugin
  inserts tmux *commands* into a running shell (visible in the
  scrollback); this plugin makes tmux the tab's argv-0 from PID 1 of
  the tab. The two plugins can coexist but tmux_mode is the one this
  scenario tests.
