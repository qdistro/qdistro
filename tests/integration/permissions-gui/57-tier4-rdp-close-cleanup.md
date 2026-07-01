# 57 — tier-4 RDP close tears down viewer, bridge, and domain

**What**: start the tier-4 RDP transport, close the visible host window,
and verify the control path tears down the FreeRDP viewer, host `socat`
bridge, guest publisher, and libvirt domain.

**Why**: the RDP path has more moving pieces than waypipe: a guest
RDP server, a guest vsock bridge, a host loopback bridge, and a FreeRDP
viewer. A visually correct window is not enough; closing it must not
leave a reachable RDP endpoint or a running VM.

Run this after scenario 56 passes on the same image.

## Setup

```bash
VM=${VMNAME:-$(virsh list --name --state-running | head -1)}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'runuser -u admin -- test -S /run/user/1000/wayland-1'
$VMEXEC "$VM" 'runuser -u admin -- pgrep -af "[q]s -p" >/dev/null'
$VMEXEC "$VM" 'test -e /dev/kvm'
$VMEXEC "$VM" 'test -f /var/lib/libvirt/images/qdistro-tier4-guest.qcow2'
$VMEXEC "$VM" 'test -d /root/qdistro-src/qdistro/tier4-vm'

VM4="qdistro-tier4-rdp-s57-$RANDOM"
echo "VM4=$VM4"
```

## Steps

### S1 — launch and wait for visible RDP window

```bash
B64=$(base64 -w0 <<EOF
rm -rf /tmp/qdistro-tier4-rdp
rm -rf /tmp/tier4-vm-guest
cp -r /root/qdistro-src/qdistro/tier4-vm /tmp/qdistro-tier4-rdp
cp -r /root/qdistro-src/qdistro/tier4-vm-guest /tmp/tier4-vm-guest
chmod -R a+rX /tmp/qdistro-tier4-rdp /tmp/tier4-vm-guest
find /tmp/qdistro-tier4-rdp /tmp/tier4-vm-guest -name '*.sh' -exec chmod a+rx {} +
setsid env TIER4_VM_NAME="$VM4" \
    TIER4_STREAMING_METHOD=rdp \
    TIER4_RDP_SUBSCRIBE=last \
    bash /tmp/qdistro-tier4-rdp/spawn-tier4.sh \
    </dev/null >/tmp/s57-tier4-rdp.log 2>&1 &
disown
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

deadline=$((SECONDS + 150))
while [ $SECONDS -lt $deadline ]; do
    if $VMEXEC "$VM" 'grep -q "launching rdp client" /tmp/s57-tier4-rdp.log'; then
        break
    fi
    sleep 2
done
sleep 5
$VMGUI "$VM" screenshot /tmp/s57-before-close.png
```

**Assert** (agent-visual): screenshot shows the RDP-backed terminal
window, same visual criteria as scenario 56.

### S2 — close using qdwin chrome

Use visual/OCR targeting. Do not hard-code pixels.

```bash
$VMGUI "$VM" screenshot /tmp/s57-close-target.png
# Runner: locate the visible qdwin close button on the RDP window titlebar.
# Click the center of that close glyph.
$VMGUI "$VM" click <close_x> <close_y>
sleep 8
$VMGUI "$VM" screenshot /tmp/s57-after-close.png
```

**Assert** (agent-visual): the RDP-backed terminal window is gone from
the screen. If a confirmation dialog appears, capture it and fail with
the screenshot; tier-4 close should be direct for this transport.

### S3 — process cleanup

```bash
$VMEXEC "$VM" "runuser -u admin -- virsh domstate $VM4 2>/dev/null || echo missing"
$VMEXEC "$VM" 'pgrep -af "tier4-.*rdp|VSOCK-CONNECT|freerdp|xfreerdp|wlfreerdp|sdl-freerdp" || true'
$VMEXEC "$VM" 'tail -80 /tmp/s57-tier4-rdp.log'
```

**Assert**:

- `virsh domstate` prints `missing` or a non-running terminal state;
- no `spawn-tier4.sh` for `$VM4` remains;
- no host `socat ... VSOCK-CONNECT` bridge for the tier-4 RDP run
  remains;
- no FreeRDP client process for this run remains.

If unrelated FreeRDP or socat processes exist from another run, do not
fail solely on `pgrep`; match against `/tmp/s57-tier4-rdp.log` paths,
the VM name, or the local RDP port recorded in the log.

### S4 — guest endpoint is unreachable

If the domain still exists but is not running, this command may print
`missing`; that is acceptable.

```bash
$VMEXEC "$VM" "runuser -u admin -- virsh qemu-agent-command $VM4 \
  '{\"execute\":\"guest-exec\",\"arguments\":{\"path\":\"/bin/sh\",\"arg\":[\"-lc\",\"pgrep -af [q]distro-forward || true; pgrep -af [q]distro-tier4-publisher || true\"]}}' \
  2>/dev/null || echo missing"
```

**Assert**: either `missing`, or no live `qdistro-forward` /
`qdistro-tier4-publisher` process is reported from the guest.

### S5 — cleanup fallback

```bash
$VMEXEC "$VM" "pkill -u root -f \"spawn-tier4.sh.*$VM4\" 2>/dev/null || true; \
               runuser -u admin -- virsh destroy $VM4 2>/dev/null || true; \
               runuser -u admin -- virsh undefine $VM4 2>/dev/null || true; \
               rm -f /home/admin/.local/share/libvirt/images/$VM4.qcow2"
```

## Report Format

Return:

- `PASS` or `FAIL`;
- before/after-close screenshots;
- `virsh domstate` result;
- any surviving process lines and why they are considered related or
  unrelated to this scenario.
