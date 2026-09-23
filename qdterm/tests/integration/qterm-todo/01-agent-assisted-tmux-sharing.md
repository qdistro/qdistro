# 01 — agent-assisted tmux sharing and screen telemetry

**What**: exercise the qterm TODO closure paths in a real GUI session:
agent control, broadcast input, tmux-native screen snapshots,
command telemetry, mosh share indicators, and SSH/web tmux transports.

This is a runner/agent scenario, not a normal pytest. Use it in the
same style as the qdistro VM GUI scenarios: commands are authoritative,
screenshots corroborate visible state.

## Setup

Run in a qdistro VM or host GUI with `tmux` installed. Optional paths
require `mosh-server` and `sshd`.

```bash
export QTERMINATOR_AGENT_CONTROL=1
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}"
SOCK="${XDG_RUNTIME_DIR:-/tmp}/qterminator-agent-$(id -u).sock"
CLI="python3 -m qterminator.plugins._agent_test_client --sock $SOCK"

mkdir -p ~/.config/qterminator
cp ~/.config/qterminator/config.toml /tmp/qterm-todo-config.backup 2>/dev/null || true
cat > ~/.config/qterminator/config.toml <<'EOF'
[plugins]
agent_control = true

[plugins.tmux_mode]
enabled = true
restore_on_start = false

[plugins.command_telemetry]
enabled = true
collect_io_bytes = true
collect_binary_breakdown = true
collect_network = true
collect_open_files = true
collect_cgroup = true
collect_syscalls = true
collect_oom = true

[plugins.tmux_share]
enabled = true
bind = "127.0.0.1"
show_qr = false
EOF

python3 -m qterminator >/tmp/qterm-todo-01.log 2>&1 &
QTERM_PID=$!
for i in $(seq 1 80); do [ -S "$SOCK" ] && break; sleep 0.1; done
```

## S1 — agent can see tmux-native screen

```bash
$CLI list > /tmp/qterm-todo-01-list.json
TID=$(python3 - <<'PY'
import json
d=json.load(open('/tmp/qterm-todo-01-list.json'))
print(d[0]['id'] if isinstance(d, list) else d['result'][0]['id'])
PY
)
$CLI attach "$TID"
$CLI send_text "$TID" "printf 'QTERM_TMUX_SCREEN\n'\n"
sleep 0.5
$CLI get_screen "$TID" > /tmp/qterm-todo-01-screen.json
```

Assert:
- `get_screen` contains `QTERM_TMUX_SCREEN`.
- The tab listing contains a non-empty `tmux_session`.
- If the agent client exposes raw JSON, `source == "tmux"` is expected
  for tmux-backed snapshots.

## S2 — broadcast does not require sendKeyEvent

Create a split, enable broadcast-all from the GUI menu, focus pane A,
type `echo BROADCAST_OK`, press Enter.

Assert:
- Both panes show `BROADCAST_OK`.
- No `sendKeyEvent` AttributeError appears in `/tmp/qterm-todo-01.log`.

Take a screenshot:

```bash
$CLI screenshot "$TID" /tmp/qterm-todo-01-broadcast.png
```

Agent visual assert: the screenshot shows two terminal panes and the
text `BROADCAST_OK` in both.

## S3 — telemetry carries extended payload

```bash
$CLI send_text "$TID" "python3 - <<'PY'\nopen('/tmp/qterm-todo-io','wb').write(b'x'*1048576)\nPY\n"
sleep 1
$CLI command_telemetry "$TID" --limit 5 > /tmp/qterm-todo-01-telemetry.json
```

Assert:
- The most recent telemetry record contains `duration`,
  `peak_rss_bytes`, `process_count`.
- It also contains at least one extended field such as `write_bytes`,
  `binary_cpu_seconds`, `open_files`, `cgroups`, `syscalls`, or
  `oom_score_max` depending on host permissions.

## S4 — tmux share surfaces

If `mosh-server` is installed, use the context menu action
`Share via Mosh...`.

Assert:
- The dialog shows a `MOSH_KEY=... mosh-client ...` connection string.
- The titlebar shows an active-share indicator (`M1`).
- `$CLI list` reports `shared_via_mosh` with the UDP port.

## S5 — SSH/web transports

If `sshd` is available, run `Share via SSH...`.
If browser sharing is enabled, run `Share in Browser...`.

Assert:
- `$CLI list` reports `shared_via_ssh` and/or `shared_via_web` ports.
- Opening the browser URL renders the tmux pane contents.
- Typing in the web terminal updates the tmux session unless configured
  read-only.

## Cleanup

```bash
kill "$QTERM_PID" 2>/dev/null || true
if [ -f /tmp/qterm-todo-config.backup ]; then
  mv /tmp/qterm-todo-config.backup ~/.config/qterminator/config.toml
else
  rm -f ~/.config/qterminator/config.toml
fi
rm -f /tmp/qterm-todo-01-*.json /tmp/qterm-todo-01-*.png /tmp/qterm-todo-01.log
```
