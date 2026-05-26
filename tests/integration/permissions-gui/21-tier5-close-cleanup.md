# 21 — closing a tier-5 toplevel tears down the guest VM cleanly

**What**: bring up a tier-5 `--vm` weston-terminal, click the close
button on its title bar (or close via the taskbar context menu),
verify the inner app exits AND the per-app guest VM domain is
destroyed + undefined AND the per-VM overlay qcow2 is unlinked. No
orphans.

**Why**: this is the orphan-resource budget for tier-5. Anything
left around after the toplevel goes away is a leak (libvirt
domain, qcow2 disk, dangling waypipe processes). The bats variant
`tests/integration/vm/s48-tier5-close-cleanup.sh` SIGTERMs the
wrapper directly — here we exercise the actual user-visible close
path through qdwin's title-bar chrome.

## Setup

```bash
VM=${VMNAME:-$(virsh list --name --state-running | head -1)}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

# Preconditions (same as scenario 20).
$VMEXEC "$VM" 'runuser -u admin -- test -S /run/user/1000/wayland-1'
$VMEXEC "$VM" 'test -e /dev/kvm'
$VMEXEC "$VM" 'test -f /var/lib/libvirt/images/qdistro-tier5-base.qcow2'
$VMEXEC "$VM" 'test -d /root/qdistro-src/qdistro/tier5-vm'

VM5="qdistro-tier5-s21-$RANDOM"
echo "VM5=$VM5"
$VMEXEC "$VM" 'pkill -u root -f spawn-tier5.sh 2>/dev/null || true; sleep 1'
```

## Steps

### S1 — spawn + wait for the toplevel to be visible

```bash
B64=$(base64 -w0 <<EOF
rm -rf /tmp/qdistro-tier5
mkdir -p /tmp/qdistro-tier5
cp -r /root/qdistro-src/qdistro/tier5-vm /tmp/qdistro-tier5/tier5-vm
cp -r /root/qdistro-src/qdistro/lib /tmp/qdistro-tier5/lib
chmod -R a+rX /tmp/qdistro-tier5
find /tmp/qdistro-tier5 -name '*.sh' -exec chmod a+rx {} +
setsid bash /tmp/qdistro-tier5/tier5-vm/spawn-tier5.sh --vm "$VM5" \
    -- weston-terminal </dev/null >/tmp/s21-spawn.log 2>&1 &
disown
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

# Wait for the toplevel to map (mirrors s20's S3 wait).
deadline=$((SECONDS + 180))
while [ $SECONDS -lt $deadline ]; do
    if $VMEXEC "$VM" 'grep -q "guest publisher pid=" /tmp/s21-spawn.log'; then
        break
    fi
    sleep 2
done
sleep 5
$VMGUI "$VM" screenshot /tmp/s21-running.png
```

**Assert** (agent-visual): `/tmp/s21-running.png` shows a
weston-terminal window labelled `[tier5:$VM5]` rendered like a
normal app window.

If S1 fails to produce a visible toplevel within ~3min, ERROR (not
FAIL) — the close path can't be exercised without something to
close.

### S2 — click the close button on the title bar

qdwin chromes tier-5 toplevels with `zxdg_decoration_manager_v1` in
server-side mode (per `qdwin/qdwin.c:62b2702`). The close button is
in the top-right corner of the title bar. OCR-find it (typically a
`×` glyph or labelled "Close" — depends on theme).

```bash
# Runner: read /tmp/s21-running.png, locate the close button on the
# tier-5 toplevel's title bar, click it.
$VMGUI "$VM" click <close_x> <close_y>
sleep 1
$VMGUI "$VM" screenshot /tmp/s21-after-close.png
```

**Assert** (agent-visual): `/tmp/s21-after-close.png` no longer
shows the weston-terminal window. The taskbar/dock has no entry
for `[tier5:$VM5]`.

If the close glyph is not detectable, fallback to a context-menu
close: right-click the toplevel's title bar → "Close" entry. Apply
the same screenshot-after assertion.

The bats variant `s48-tier5-close-cleanup.sh` exercises the same
trap via SIGTERM directly; it's the regression net underneath this
scenario.

### S3 — verify the wrapper exited

```bash
$VMEXEC "$VM" 'pgrep -af "spawn-tier5.sh.*'"$VM5"'" || echo "no spawn-tier5 wrapper running (expected)"'
```

**Assert**: no `spawn-tier5.sh` process remains for `$VM5`. The
wrapper's `wait $CLIENT_PID` returns when waypipe-client tears down
its side after the toplevel goes away.

### S4 — verify libvirt domain is gone

```bash
$VMEXEC "$VM" "runuser -u admin -- virsh dominfo $VM5 2>&1 || echo no-such-domain"
```

**Assert**: output contains `no-such-domain` (or
`error: failed to get domain`) — i.e., the domain is gone, not
merely stopped. The trap in `spawn-tier5.sh` does both `virsh destroy`
and `virsh undefine`, so a stopped-but-still-defined domain is a
**leak**, not a pass.

### S5 — verify the overlay qcow2 is unlinked

```bash
$VMEXEC "$VM" "ls /home/admin/.local/share/libvirt/images/$VM5.qcow2 2>&1 || echo overlay-gone"
```

**Assert**: output contains `No such file or directory` or
`overlay-gone`. The base image
(`/var/lib/libvirt/images/qdistro-tier5-base.qcow2`) MUST still be
present — we only reap the per-VM overlay. Cross-check:

```bash
$VMEXEC "$VM" 'ls -lh /var/lib/libvirt/images/qdistro-tier5-base.qcow2'
```

### S6 — cleanup (defensive)

```bash
$VMEXEC "$VM" "pkill -u root -f \"spawn-tier5.sh.*$VM5\" 2>/dev/null || true; \
               runuser -u admin -- virsh destroy $VM5 2>/dev/null || true; \
               runuser -u admin -- virsh undefine $VM5 2>/dev/null || true; \
               rm -f /home/admin/.local/share/libvirt/images/$VM5.qcow2"
```

## Known caveats

- **Title-bar close button position** depends on the active qdshell
  theme. OCR/vision identification, NOT hard-coded pixel offsets.
- **xdg_toplevel.close semantics**: weston-terminal listens for
  the close request and exits cleanly. Some inner apps ignore the
  request — for those, the scenario falls back to forcing the close
  by killing the in-guest waypipe-server (out of scope here).
- **Trap reliability**: if `spawn-tier5.sh` is killed with SIGKILL
  the trap doesn't run and the domain WILL leak. This scenario
  exercises a clean close (SIGTERM via toplevel close), not a kill.
  The bats s37 variant covers SIGTERM specifically.
- **TIER5_KEEP_DOMAIN=1** intentionally skips cleanup for debug.
  This scenario assumes the env var is unset; setup leaves it alone.
