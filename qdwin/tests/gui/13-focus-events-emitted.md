# 13 — qdwin emits focus-change events for every keyboard-focus move

<!-- qci:visual: none -->

**Acceptance criterion:** every keyboard-focus transition between
toplevels — spawn, alt-tab, click-to-focus, last-window-close — emits
a `qdwin: focus handle=N (was M) seat=…` line in the journal, even
when qdshell hasn't yet bound `qdwin_shell_v1` at v14+. Without these
lines, the shell's window-list focus highlight can drift silently and
focus handoff bugs become invisible to the regression suite (see
`todo/qdwin-focus-events.md`).

## Setup

```bash
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"

pgrep -f "http.server 8765" >/dev/null || (
    cd ${QDWIN_REPO} && \
    python3 -m http.server 8765 --bind 127.0.0.1 >/tmp/qdistro-http.log 2>&1 &
)
sleep 1
qdwin_session_healthy || { echo "FAIL: session not up"; exit 1; }

"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -x foot 2>/dev/null; sleep 1' >/dev/null
```

## Steps

### Step 1 — baseline: zero foots, capture journal cursor

```bash
CURSOR=$("$QDWIN_VM_EXEC" "$VMNAME" "journalctl _UID=1000 -n 1 \
  --show-cursor --no-pager 2>/dev/null | tail -1 | sed 's/^-- cursor: //'")
echo "cursor=$CURSOR"
```

### Step 2 — spawn foot 1; expect a focus event for the new handle

```bash
"$QDWIN_VM_EXEC" "$VMNAME" \
  "runuser -l admin -c 'XDG_RUNTIME_DIR=/run/user/1000 \
   WAYLAND_DISPLAY=wayland-1 foot sleep 600 &' " >/dev/null
sleep 2
"$QDWIN_VM_EXEC" "$VMNAME" \
  "journalctl _UID=1000 _SYSTEMD_USER_UNIT=qdwin-compositor.service --after-cursor='$CURSOR' --no-pager | \
   grep -E 'qdwin: focus handle='"
```

**Assert (2.1):** at least one `qdwin: focus handle=H (was 4294967295) seat=...`
line, where H is the toplevel handle of the new foot
(`was=4294967295` because UINT32_MAX represents "no previous focus").

### Step 3 — spawn foot 2; expect a transition between handles

```bash
"$QDWIN_VM_EXEC" "$VMNAME" \
  "runuser -l admin -c 'XDG_RUNTIME_DIR=/run/user/1000 \
   WAYLAND_DISPLAY=wayland-1 foot sleep 600 &' " >/dev/null
sleep 2
qdwin_ctrl "list"
"$QDWIN_VM_EXEC" "$VMNAME" \
  "journalctl _UID=1000 _SYSTEMD_USER_UNIT=qdwin-compositor.service --after-cursor='$CURSOR' --no-pager | \
   grep -E 'qdwin: focus handle=' | tail -2"
```

**Assert (3.1):** the latest focus line reports `handle=H2 (was H1)`
where H2 is the new foot's handle and H1 is foot 1's. The `was=`
field proves the dedup-on-same-handle guard is working AND the
previous handle is being tracked.

### Step 4 — close foot 2; expect focus to drop or transfer

```bash
"$QDWIN_VM_EXEC" "$VMNAME" "pkill -9 -n foot" >/dev/null
sleep 1
"$QDWIN_VM_EXEC" "$VMNAME" \
  "journalctl _UID=1000 _SYSTEMD_USER_UNIT=qdwin-compositor.service --after-cursor='$CURSOR' --no-pager | \
   grep -E 'qdwin: focus handle=' | tail -1"
```

**Assert (4.1):** a new focus line appears after the kill. The new
handle is either H1 (focus transferred to remaining foot) or
4294967295 (focus dropped — libweston did not auto-transfer).

### Step 5 — close last foot; focus must drop to UINT32_MAX

```bash
"$QDWIN_VM_EXEC" "$VMNAME" "pkill -9 -x foot" >/dev/null
sleep 1
"$QDWIN_VM_EXEC" "$VMNAME" \
  "journalctl _UID=1000 _SYSTEMD_USER_UNIT=qdwin-compositor.service --after-cursor='$CURSOR' --no-pager | \
   grep -E 'qdwin: focus handle=' | tail -1"
```

**Assert (5.1):** the final focus line is `handle=4294967295 (was H1)`.
A stuck focus on the now-destroyed handle would mean the seat tracker
never observed the surface destroy → the shell could keep highlighting
a dead window.

## Cleanup

```bash
"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -x foot 2>/dev/null; true' >/dev/null
```

## Pass criteria

All asserts 2.1 → 5.1 pass. Confirms qdwin's keyboard-focus listener
is installed on all seats and emits a ground-truth log line on every
transition, independent of shell-binding state.

## Known-broken-if

- Step 2 produces no `qdwin: focus handle=` line → either
  `kbd_focus_listener` isn't installed (no keyboard at seat-init time
  and no re-trigger), or `qdwin_seat_emit_focus_now` is gated. Check
  `qdwin_install_focus_listener_if_needed` in `qdwin/qdwin.c`.
- Step 5 final line is not 4294967295 → libweston isn't clearing
  keyboard focus on surface destroy, OR `qdwin_toplevel_by_surface`
  returns a stale tl for the destroyed surface. Look at the toplevel
  list cleanup in `surface_removed`.
- Lines appear with `seat_focus_changed` text but NOT `focus handle=`
  → the unconditional log inside `qdwin_seat_emit_focus_now` was
  reverted. Compare against the protocol-bound emission in
  `qdwin_emit_seat_focus_changed`.
