# 01 — shell_integration parses real OSC 133 from a real shell

**What**: enable `shell_integration` + `agent_control`, point a sandboxed
bash at the installed hook script, drive a few commands through
`agent_control` (`send_text`), and assert via `rpc_command_history`
that the plugin recorded prompts, exit codes, and the CWD reported by
OSC 7.

**Why**: the unit tests synthesize OSC sequences directly. This scenario
exercises the path that matters in production: a real shell process
emits the sequences during its real lifecycle (precmd / preexec /
PROMPT_COMMAND), the kernel pipes them through the PTY, and the
shadow-screen listener regex picks them up out of a real byte stream
with all of the noise (cursor moves, color SGR, etc.) that comes with
a real prompt.

## Setup

Runs on the host. Uses `QT_QPA_PLATFORM=offscreen` so it works in CI.

```bash
export QT_QPA_PLATFORM=${QT_QPA_PLATFORM:-offscreen}
export QTERMINATOR_AGENT_CONTROL=1

# Install the bash hook to a known location, source via a custom rc.
HOOKDIR="$(mktemp -d)"
export XDG_DATA_HOME="$HOOKDIR"
python3 -m qterminator.shell_integration_cli print bash > "$HOOKDIR/qterminator/shell-integration.bash"

# Build a throwaway .bashrc that sources the hook.
BASHRC="$(mktemp)"
cat > "$BASHRC" <<EOF
PS1='\\u@\\h \\W \\$ '
source "$HOOKDIR/qterminator/shell-integration.bash"
EOF

# Spawn qterminator with this rc — the shell it inherits will emit
# OSC 133/7 on every command. We use --shell so the in-tab program is
# our custom bash, not the user's default.
SOCK="${XDG_RUNTIME_DIR:-/tmp}/qterminator-agent-$(id -u).sock"
rm -f "$SOCK"
python3 -m qterminator -e "bash --rcfile '$BASHRC' -i" >/tmp/shell-integration-01.log 2>&1 &
QT_PID=$!

for i in $(seq 1 30); do [ -S "$SOCK" ] && break; sleep 0.2; done
test -S "$SOCK" || { kill $QT_PID; exit 2; }

CLI="python3 -m qterminator.plugins._agent_test_client --sock $SOCK"
```

## Steps

### S1 — drive `ls; false; echo done` through the bash session

```bash
TID=$($CLI list | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')
$CLI attach $TID
$CLI send $TID $'ls\n'
sleep 0.3
$CLI send $TID $'false\n'
sleep 0.3
$CLI send $TID $'echo done\n'
sleep 0.5
```

### S2 — `command_history` reports three records with correct exits

```bash
$CLI command_history $TID --limit 10 > /tmp/shell-integration-01-history.json
```

**Assert:**
- the returned `records` array has length 3.
- exit statuses, in order, are `[0, 1, 0]` (ls succeeds, false exits 1,
  echo succeeds).
- `cwd_reported` in the response is the user's actual `$PWD` (or
  wherever `bash` started — verify it's an absolute path).
- the `output_seq_range` of each record is monotonic across records
  (later commands have later sequence numbers).

### S3 — `list_tabs.last_command` matches the latest record

```bash
$CLI list > /tmp/shell-integration-01-list.json
```

**Assert:**
- `last_command.exit_status == 0` (matches `echo done`)
- `cwd_reported` matches what `command_history` reported.

### S4 — `cd /tmp` updates `cwd_reported`

```bash
$CLI send $TID $'cd /tmp\n'
sleep 0.4
$CLI list > /tmp/shell-integration-01-list-after-cd.json
```

**Assert:**
- `cwd_reported == "/tmp"` (or the resolved real path if /tmp is a
  symlink — the hook emits the resolved `$PWD`).

## Teardown

```bash
kill $QT_PID 2>/dev/null; wait $QT_PID 2>/dev/null
rm -rf "$HOOKDIR" "$BASHRC" /tmp/shell-integration-01-*.json /tmp/shell-integration-01.log
rm -f "$SOCK"
```

## Notes for the runner

- **Why not `bash-preexec.sh`?** Our bash hook ships its own minimal
  precmd/preexec wiring via `PROMPT_COMMAND` so it works without an
  external dependency. If a user already uses bash-preexec the hook
  will piggy-back on `preexec_functions`.
- **PS1 must end in `;B`.** The hook injects `OSC 133 ;B` at the tail
  of `$PS1` exactly once. If the user's prompt is itself dynamic
  (powerlevel10k-style fancy prompt) the injection still works
  because it's appended *after* whatever the user set.
- **The unit tests cover edge cases.** This scenario only proves the
  round-trip — partial OSC sequences across read boundaries, BEL vs
  ESC-\\ terminators, OSC 8 hyperlink ranges are all covered by
  `tests/test_shell_integration.py`.
