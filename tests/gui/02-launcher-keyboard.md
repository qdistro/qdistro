# 02 — launcher accepts keyboard input (B3 fix verification)

<!-- qci:visual: required -->

**Acceptance criterion:** the user can open the launcher overlay
(Ctrl+Space), type to filter, navigate with arrow keys, and press
Enter to spawn — all from a real keyboard, not the ctrl-socket.
Validates qdwin_shell_v1 v17 `overlay_key` event delivery for
`role=launcher`.

## Setup

```bash
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"

pgrep -f "http.server 8765" >/dev/null || (
    cd ${QDWIN_REPO} && \
    python3 -m http.server 8765 --bind 127.0.0.1 >/tmp/qdistro-http.log 2>&1 &
)
sleep 1

qdwin_session_healthy || { echo "FAIL: qdshell ctrl-socket not reachable"; exit 1; }

# Ensure no foots are alive (clean baseline).
cat > ${QDWIN_REPO}/extra/cleanfoots.sh <<'EOF'
pkill -u admin -x foot 2>/dev/null
sleep 1
EOF
"$QDWIN_VM_EXEC" "$VMNAME" 'wget -qO /tmp/cf.sh http://10.0.2.2:8765/extra/cleanfoots.sh && bash /tmp/cf.sh' >/dev/null
```

## Steps

### Step 1 — open launcher with Ctrl+Space

```bash
qdwin_chord ctrl -- spc
sleep 0.5
qdwin_screenshot /tmp/02-step1-open.png
qdwin_ctrl "launcher"
```

**Assert (1.1):** ctrl-socket reports `visible=True indexed>=4`.
**Assert (1.2):** screenshot shows the launcher overlay (centred dark
rectangle) with `> _` prompt and entry list.

### Step 2 — type "foot" to filter

```bash
for c in f o o t; do
    qdwin_qmp_key "$c" down; sleep 0.05
    qdwin_qmp_key "$c" up; sleep 0.05
done
sleep 0.3
qdwin_screenshot /tmp/02-step2-filtered.png
qdwin_ctrl "launcher"
```

**Assert (2.1):** ctrl-socket reports `filter='foot' matches=3`.
**Assert (2.2):** screenshot shows `> foot_` in the prompt bar and
the visible list shrinks to 3 entries (Foot / Foot Client / Foot
Server).

### Step 3 — navigate with arrow keys

```bash
qdwin_send_key KEY_DOWN
sleep 0.2
qdwin_screenshot /tmp/02-step3-down.png
qdwin_ctrl "launcher"
```

**Assert (3.1):** `selection=1` (was 0). Highlighted row in the
screenshot is "Foot Client", not "Foot".

### Step 4 — Esc dismisses (and re-open + Enter to spawn)

```bash
qdwin_send_key KEY_ESC
sleep 0.3
qdwin_ctrl "launcher"
```

**Assert (4.1):** ctrl-socket reports `visible=False`.

```bash
qdwin_chord ctrl -- spc
sleep 0.3
for c in f o o t; do
    qdwin_qmp_key "$c" down; sleep 0.05
    qdwin_qmp_key "$c" up; sleep 0.05
done
sleep 0.2
qdwin_send_key KEY_ENTER
sleep 1.5
qdwin_screenshot /tmp/02-step4-spawned.png
qdwin_ctrl "list"
```

**Assert (4.2):** `list` reports at least one toplevel.
**Assert (4.3):** screenshot shows a foot terminal window with
`admin@localhost:~>` prompt.

### Step 5 — typing reaches the focused terminal

```bash
qdwin_type_lower "echo from launcher"
qdwin_send_key KEY_ENTER
sleep 1
qdwin_screenshot /tmp/02-step5-typed.png
```

**Assert (5.1):** screenshot shows `echo from launcher` typed at the
prompt and `from launcher` echoed back on the next line.

## Cleanup

```bash
"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -x foot 2>/dev/null; true' >/dev/null
```

## Pass criteria

All asserts 1.1 → 5.1 pass. Confirms B3 fix is live: launcher accepts
keyboard input via the qdwin overlay grab + qdwin_shell_v1.overlay_key
event path.

## Known-broken-if

- `matches=0` after typing `foot` → overlay_key event isn't reaching
  qdshell. Check `bound qdwin_shell_v1 v17` line in qdshell.log; if
  it says `v16`, the protocol header wasn't regenerated (re-run
  `gen_protocol.sh` against the v17 XML).
- `selection` doesn't change after KEY_DOWN → launcher_nav handler
  isn't wired. Check `qdshell shell.qml` `on_overlay_key`
  for the XKB_Down branch.
