# 56 — tier-4 RDP transport maps one guest window on the host

**What**: start a tier-4 VM with `TIER4_STREAMING_METHOD=rdp`, let the
guest publisher subscribe to the latest guest qdwin toplevel, and verify
the host sees a normal chromed window rendered through FreeRDP.

**Why**: unit tests cover command construction and credential handling.
This scenario verifies the visible end-to-end path: guest qdwin
PipeWire output -> `qdistro-forward` RDP server -> guest `socat`
vsock bridge -> host loopback bridge -> FreeRDP client -> host qdwin
toplevel.

Budget: 3-4 minutes wall-clock. Guest boot and first PipeWire/RDP frame
are the slow parts.

## Setup

```bash
VM=${VMNAME:-$(virsh list --name --state-running | head -1)}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

# Precondition: outer compositor + qdshell up.
$VMEXEC "$VM" 'runuser -u admin -- test -S /run/user/1000/wayland-1'
$VMEXEC "$VM" 'runuser -u admin -- pgrep -af 'qs -p' >/dev/null'

# Precondition: nested virt + tier-4 image + source tree.
$VMEXEC "$VM" 'test -e /dev/kvm'
$VMEXEC "$VM" 'test -f /var/lib/libvirt/images/qdistro-tier4-guest.qcow2'
$VMEXEC "$VM" 'test -d /root/qdistro-src/qdistro/tier4-vm'

# Precondition: host-side RDP client exists inside the outer VM.
$VMEXEC "$VM" 'command -v wlfreerdp >/dev/null 2>&1 || \
               command -v xfreerdp3 >/dev/null 2>&1 || \
               command -v xfreerdp >/dev/null 2>&1 || \
               command -v sdl-freerdp >/dev/null 2>&1'

VM4="qdistro-tier4-rdp-s56-$RANDOM"
echo "VM4=$VM4"

# Drain leftovers from prior runs.
$VMEXEC "$VM" 'pkill -u root -f "spawn-tier4.sh.*rdp" 2>/dev/null || true; sleep 1'
```

This scenario requires the tier-4 outer stack: the qdwin/qdshell
compositor on `wayland-1`, nested KVM, and the baked tier-4 guest image.
When the OUTER stack itself is unprovisioned in this VM profile (no
qdwin/qdshell session on `wayland-1`, or no nested KVM), the GUI gate
short-circuits to **SKIP** before dispatching this scenario — mirroring
the bats `tiered-isolation` skip. When the outer stack IS present but
only the tier-4 guest image is missing/broken, report **INFRA** and
point at `tier4-vm-guest/build-guest-image.sh`. Do not silently skip a
present-but-broken stack; this is a release-gating visual scenario for
RDP.

## Steps

### S1 — start tier-4 with RDP selected

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
    TIER4_DEBUG=1 \
    bash /tmp/qdistro-tier4-rdp/spawn-tier4.sh \
    </dev/null >/tmp/s56-tier4-rdp.log 2>&1 &
disown
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: `/tmp/s56-tier4-rdp.log` eventually contains:

```bash
deadline=$((SECONDS + 120))
while [ $SECONDS -lt $deadline ]; do
    if $VMEXEC "$VM" 'grep -q "starting guest RDP publisher" /tmp/s56-tier4-rdp.log'; then
        break
    fi
    sleep 2
done
$VMEXEC "$VM" 'grep "starting guest RDP publisher" /tmp/s56-tier4-rdp.log'
```

### S2 — wait for the RDP bridge and client launch

```bash
deadline=$((SECONDS + 120))
while [ $SECONDS -lt $deadline ]; do
    if $VMEXEC "$VM" 'grep -q "launching rdp client" /tmp/s56-tier4-rdp.log'; then
        break
    fi
    sleep 2
done
$VMEXEC "$VM" 'tail -80 /tmp/s56-tier4-rdp.log'
```

**Assert**: the log contains all of:

- `publisher vsock bind confirmed`
- `launching rdp client`
- `display-client log:`

If it instead contains `guest RDP publisher did not write
/run/qdistro-tier4-rdp.env`, fail and attach both the spawn log and
`/var/log/qdistro-tier4-rdp-publisher.launch.log` from the guest via
`virsh qemu-agent-command` if available.

### S3 — visual assertion: one RDP-backed guest window is visible

```bash
sleep 5
$VMGUI "$VM" screenshot /tmp/s56-tier4-rdp-visible.png
```

**Assert** (agent-visual): the screenshot shows a normal qdwin-chromed
window whose content is a guest `weston-terminal` rendered through the
RDP viewer. Expected visual cues:

- a terminal-like content area is visible, not a blank black rectangle;
- the host chrome/title area is present, so the RDP viewer is a normal
  host-side toplevel;
- no password, RDP credential text, or `/p:` argument appears on screen.

The exact FreeRDP window title is client-dependent; do not require a
literal title string. The load-bearing assertion is visible terminal
content inside host qdwin chrome.

### S4 — journal and process cross-checks

```bash
$VMEXEC "$VM" "journalctl --since '3min ago' | grep -E 'qdwin: view_stream approved|stream_input claim OK' | tail -20"
$VMEXEC "$VM" 'pgrep -af "socat.*VSOCK-CONNECT|freerdp|xfreerdp|wlfreerdp|sdl-freerdp"'
```

**Assert**: journal contains `view_stream approved` and either
`stream_input claim OK` or no input-claim line yet. Treat missing
`stream_input claim OK` as **WARN** unless S3 is blank; some FreeRDP
clients delay input channel activity until focus.

### S5 — cleanup

```bash
$VMEXEC "$VM" "pkill -u root -f \"spawn-tier4.sh.*$VM4\" 2>/dev/null || true; sleep 3"
$VMEXEC "$VM" "runuser -u admin -- virsh destroy $VM4 2>/dev/null || true; \
               runuser -u admin -- virsh undefine $VM4 2>/dev/null || true; \
               rm -f /home/admin/.local/share/libvirt/images/$VM4.qcow2"
```

## Report Format

Return:

- `PASS` or `FAIL`;
- screenshot paths for S3;
- the last 80 lines of `/tmp/s56-tier4-rdp.log` on failure;
- explicit `INFRA` if the guest image, nested KVM, or FreeRDP client is
  missing.
