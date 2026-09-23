# 03 — locker accepts keyboard + unlock returns desktop (B1+B4)

<!-- qci:visual: required -->

**Acceptance criterion:** Ctrl+Alt+L locks the desktop; the user can
type a password into the lock surface (B4); a correct password
unlocks the session AND tears the lock surface down (B1); subsequent
typing reaches the previously-focused toplevel (regression guard
against the "lock-unlock breaks keyboard" bug).

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

# Drain any stale lock surface (B1 didn't always fire pre-task(126)).
LK=$(qdwin_ctrl "locker")
case "$LK" in *"attached=yes"*)
    "$QDWIN_VM_EXEC" "$VMNAME" 'systemctl restart greetd-qdwin.service; sleep 3' >/dev/null
    qdwin_session_healthy || { echo "FAIL: session not back"; exit 1; }
    ;;
esac

# Spawn a foot to verify post-unlock keyboard works.
"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -x foot 2>/dev/null; sleep 1' >/dev/null
qdwin_ctrl "launcher-toggle" >/dev/null
qdwin_ctrl "launcher-type foot" >/dev/null
qdwin_ctrl "launcher-activate" >/dev/null
sleep 1.5
```

## Steps

### Step 1 — pre-lock typing reaches foot

```bash
qdwin_type_lower "echo before"
qdwin_send_key KEY_ENTER
sleep 1
qdwin_screenshot /tmp/03-step1-pre.png
```

**Assert (1.1):** screenshot shows `echo before` and `before` output
in foot. Confirms baseline keyboard reaches the focused toplevel.

### Step 2 — Ctrl+Alt+L engages locker

```bash
qdwin_chord ctrl alt -- l
sleep 1
qdwin_screenshot /tmp/03-step2-locked.png
qdwin_ctrl "locker"
```

**Assert (2.1):** ctrl-socket reports `locked=True attached=yes
reason=manual`.
**Assert (2.2):** screenshot shows the lock surface: clock, date,
`admin` username, `Password` field, `Locked — reason=manual` status.

### Step 3 — type the password (B4 — overlay grab forwards keys)

```bash
qdwin_qmp_key shift down; sleep 0.03
qdwin_qmp_key p down; sleep 0.05
qdwin_qmp_key p up; sleep 0.05
qdwin_qmp_key shift up; sleep 0.03
qdwin_qmp_key a down; sleep 0.05
qdwin_qmp_key a up; sleep 0.05
qdwin_qmp_key shift down; sleep 0.03
qdwin_qmp_key minus down; sleep 0.05
qdwin_qmp_key minus up; sleep 0.05
qdwin_qmp_key shift up; sleep 0.03
for c in s s w 0 r d 4 5; do
    qdwin_qmp_key "$c" down; sleep 0.05
    qdwin_qmp_key "$c" up; sleep 0.05
done
sleep 0.3
qdwin_screenshot /tmp/03-step3-typed.png
qdwin_ctrl "locker"
```

**Assert (3.1):** ctrl-socket reports `prompt-len=11`.
**Assert (3.2):** screenshot shows 11 password dots in the
password field — confirms the overlay_key event delivered each
character into the locker prompt buffer.

### Step 4 — Enter unlocks AND removes lock surface (B1)

```bash
qdwin_send_key KEY_ENTER
sleep 1.5
qdwin_screenshot /tmp/03-step4-unlocked.png
qdwin_ctrl "locker"
```

**Assert (4.1):** ctrl-socket reports `locked=False attached=no`. The
`attached=no` is the B1 fix — the lock surface proxy is destroyed,
not just the locked flag flipped.
**Assert (4.2):** screenshot shows the original foot terminal (no
lock surface).

### Step 5 — post-unlock typing reaches foot (B4 grab end-on-resource-destroy)

```bash
qdwin_type_lower "echo after"
qdwin_send_key KEY_ENTER
sleep 1
qdwin_screenshot /tmp/03-step5-post.png
qdwin_ctrl "locker"
```

**Assert (5.1):** screenshot shows BOTH `echo before / before` AND
`echo after / after` in the foot. Both round-trips visible.
**Assert (5.2):** locker still reports `attached=no` (no spurious
re-lock from stuck modifiers).

## Cleanup

```bash
"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -x foot 2>/dev/null; true' >/dev/null
```

## Pass criteria

All asserts 1.1 → 5.2 pass. Confirms B1 (lock_surface destroyed on
unlock), B4 (locker overlay accepts keyboard), and the regression
guard against "ctrl/alt held from chord poisoning subsequent KEY_ENTER"
(fixed by routing all input through QMP — see qdwin-helpers.sh:75).

## Known-broken-if

- Step 4 PASS at ctrl-socket (`locked=False`) but FAIL at screenshot
  (lock screen still visible) → B1 regression. The `unlock()` flow in
  `qdshell/Modules/LockScreen (QML)` must call `lk.proxy.destroy()`.
- Step 5 produces `^[[13;7~` or similar escape sequences in the
  terminal output → modifier state poisoning. Helpers must use QMP
  `input-send-event` for ALL input (not virsh send-key for some and
  QMP for others); see qdwin_send_key / qdwin_type_lower.
- Step 5.2 shows `attached=yes` with locked=True → re-lock fired on
  one of the typed chars. Check that the helper isn't holding ctrl
  or alt across calls (qdwin_chord must release modifiers in reverse
  order at end).
