# 20 — tier-5 `--vm` cold-start UX, placeholder → real toplevel

**What**: spawn weston-terminal in a real per-app guest VM via
`spawn-tier5.sh --vm`; verify the cold-start UX (placeholder taskbar
entry / blank chromed view) renders during guest boot, then resolves
to a real toplevel once the in-guest waypipe-server connects back
across vsock.

**Why**: this is the visual half of the tier-5 `--vm` contract.
`tests/integration/vm/s45-tier5-vm.sh` covers the wire (domain up,
overlay disk linked-clone, qga publisher triggered, qdwin secctx
arrival). Here we corroborate visually that the user-visible UX —
a window placeholder during the ~30-60s guest boot — works the way
the cold-start spec in `doc/window-hierarchy.md` describes.

This scenario REQUIRES the tier-5 outer stack: the qdwin/qdshell
compositor on `wayland-1`, nested KVM, and the tier-5 base image
(`/var/lib/libvirt/images/qdistro-tier5-base.qcow2`). When the OUTER
stack itself is unprovisioned in this VM profile (no qdwin/qdshell
session on `wayland-1`, or no nested KVM), the GUI gate short-circuits to
**SKIP** before dispatching this scenario — mirroring the bats
`tiered-isolation` skip. The tier-5 base image is OPT-IN
(`QDISTRO_BUILD_TIER5_BASE=1`): when the outer stack is present but the
base image is absent and the run did **not** opt in, the gate also
**SKIPs** (a clean "not built" rather than a noisy ERROR every default
run). Only when the run DID opt in (env set) but the bake is still
missing/broken does the runner report **ERROR** with a one-line note
pointing at `tier5-vm/build-guest-image.sh` — do not silently skip a
bake the run explicitly requested.

Budget: this scenario can take 2-3 minutes wall-clock (guest boot +
waypipe handshake + ~5s for placeholder UI verification).

## Setup

```bash
VM=${VMNAME:-$(virsh list --name --state-running | head -1)}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

# Precondition: outer compositor + qdshell up.
$VMEXEC "$VM" 'runuser -u admin -- test -S /run/user/1000/wayland-1'
$VMEXEC "$VM" 'runuser -u admin -- pgrep -af 'qs -p' >/dev/null'

# Precondition: nested KVM + base image + tier-5 source.
$VMEXEC "$VM" 'test -e /dev/kvm'
$VMEXEC "$VM" 'test -f /var/lib/libvirt/images/qdistro-tier5-base.qcow2'
$VMEXEC "$VM" 'test -d /root/qdistro-src/qdistro/tier5-vm'

# Pick a unique VM name so re-runs don't collide.
VM5="qdistro-tier5-s20-$RANDOM"
echo "VM5=$VM5"

# Drain any leftover tier-5 spawn.
$VMEXEC "$VM" 'pkill -u root -f spawn-tier5.sh 2>/dev/null || true; sleep 1'
```

## Steps

### S1 — spawn tier-5 `--vm`; placeholder should appear immediately

The host-side waypipe-client listens on vsock CID=2 as soon as
spawn-tier5.sh starts; qdshell can show a placeholder taskbar entry
before the guest finishes booting. We snapshot UX state at three
points:

```bash
B64=$(base64 -w0 <<EOF
rm -rf /tmp/qdistro-tier5
mkdir -p /tmp/qdistro-tier5
cp -r /root/qdistro-src/qdistro/tier5-vm /tmp/qdistro-tier5/tier5-vm
cp -r /root/qdistro-src/qdistro/lib /tmp/qdistro-tier5/lib
chmod -R a+rX /tmp/qdistro-tier5
find /tmp/qdistro-tier5 -name '*.sh' -exec chmod a+rx {} +
# TIER5_MEM_KIB=524288 (512 MiB): spawn-tier5.sh defaults to 1.5 GiB, which
# overcommits the nested CI VM and kills the guest mid-boot ("domain stopped").
# Mirror the s45-tier5-vm.sh CI accommodation so the nested guest fits.
TIER5_MEM_KIB=524288 setsid bash /tmp/qdistro-tier5/tier5-vm/spawn-tier5.sh --vm "$VM5" \
    -- weston-terminal </dev/null >/tmp/s20-spawn.log 2>&1 &
disown
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
sleep 3
$VMGUI "$VM" screenshot /tmp/s20-cold.png
```

**Assert** (agent-visual): screenshot `/tmp/s20-cold.png` shows
either:
- a placeholder taskbar/dock entry for the tier-5 app (reduced
  opacity, spinner overlay) — if VMApps.qml is wired, OR
- no visible weston-terminal yet (acceptable fallback until
  VMApps.qml lands; the guest hasn't connected yet so there's no
  toplevel to render).

Report which fallback is observed. If neither — i.e., qdshell is
showing a stale/crashed state — FAIL.

### S2 — wait for guest boot; verify domain reaches running

Budget 90s.

```bash
saw_running=0
deadline=$((SECONDS + 90))
while [ $SECONDS -lt $deadline ]; do
    state=$($VMEXEC "$VM" "runuser -u admin -- virsh domstate $VM5 2>/dev/null || true" | tr -d '[:space:]')
    case "$state" in
        running) saw_running=1; break;;
    esac
    sleep 2
done

# Tolerant re-confirm: a transient runuser/virsh transport blip under full-run load
# must NOT read as "shut off" (the wait loop above already observed running, and S3/S4
# prove liveness). Accept any live state; only treat an EXPLICIT terminal state seen on
# CONSECUTIVE reads as real. Note: tr strips the space so "shut off" -> "shutoff".
final=""
terminal_hits=0
for _ in 1 2 3 4 5; do
    s=$($VMEXEC "$VM" "runuser -u admin -- virsh domstate $VM5 2>/dev/null || true" | tr -d '[:space:]')
    case "$s" in
        running|paused|idle|pmsuspended) final="$s"; terminal_hits=0; break;;
        shutoff|crashed) final="$s"; terminal_hits=$((terminal_hits + 1)); [ "$terminal_hits" -ge 2 ] && break; sleep 1;;
        "") sleep 1;;
        *) final="$s"; sleep 1;;
    esac
done
```

**Assert**: `saw_running=1` AND `final` is NOT a confirmed terminal
state (i.e. not `shutoff`/`crashed` seen on consecutive reads). FAIL
if the wait loop never observed `running` (`saw_running=0`) — even if
later queries are empty/transient — and attach the spawn log. A lone
transient empty/error after a confirmed `running` is NOT a failure.

### S3 — wait for waypipe handshake; weston-terminal toplevel appears

The host-side log line `[tier5] guest publisher pid=` confirms the
in-guest publisher started. Budget 90s after S2.

```bash
deadline=$((SECONDS + 90))
while [ $SECONDS -lt $deadline ]; do
    if $VMEXEC "$VM" 'grep -q "guest publisher pid=" /tmp/s20-spawn.log'; then
        break
    fi
    sleep 2
done
$VMEXEC "$VM" 'tail -20 /tmp/s20-spawn.log'
sleep 5  # allow waypipe handshake + first frame
$VMGUI "$VM" screenshot /tmp/s20-warm.png
```

**Assert** (agent-visual): `/tmp/s20-warm.png` shows the
weston-terminal at full opacity, rendered like an ordinary app
window. Title bar (or taskbar entry) shows the title-prefix
`[tier5:$VM5]`.

If a tier-5 silo-badge has landed in qdshell by the time this
scenario is run, also assert: the badge ring colour matches the
tier-5 spec in `doc/ui.md` "silo-badges" (distinct from tier-2's
colour).

### S4 — input through the per-app VM

```bash
# OCR-find the terminal; click its content area.
$VMGUI "$VM" click <cx> <cy>
sleep 0.3
$VMEXEC "$VM" 'runuser -u admin -- ydotool type "echo from-tier5-vm"'
sleep 0.2
$VMEXEC "$VM" 'runuser -u admin -- ydotool key enter'
sleep 1
$VMGUI "$VM" screenshot /tmp/s20-typed.png
```

**Assert** (agent-visual): the typed text appears on a new line in
the terminal. This proves input event → host waypipe-client →
vsock → in-guest waypipe-server → in-guest weston-terminal works
end-to-end. Soft-pass if ydotool unavailable (see caveats).

### S5 — journal cross-check (load-bearing)

```bash
$VMEXEC "$VM" "journalctl --since '3min ago' | grep -E 'qdwin:.*qdistro\.tier5\.' | head -20"
```

**Assert**: at least one matching line. If logging is sparse, note
it but don't FAIL — S3's visual confirmation is the load-bearing
assertion for this scenario.

### S6 — cleanup

```bash
$VMEXEC "$VM" "pkill -u root -f \"spawn-tier5.sh.*$VM5\" 2>/dev/null || true; sleep 2"
$VMEXEC "$VM" "runuser -u admin -- virsh destroy $VM5 2>/dev/null || true; \
               runuser -u admin -- virsh undefine $VM5 2>/dev/null || true; \
               rm -f /home/admin/.local/share/libvirt/images/$VM5.qcow2"
```

## Known caveats

- **Guest boot can be slow** the first time the linked-clone
  initialises its journal / regenerates SSH host keys / sparsifies.
  Budget 90s in S2; if regularly slower, bump and re-time.
- **VMApps.qml not yet on main** as of 2026-05-15. Until it ships,
  the cold-start placeholder fallback in S1 is "no visible toplevel
  yet" rather than "spinner overlay". Once it lands, tighten S1.
- **Silo badge colour not yet rendered.** Same blocker as s18 —
  delegate-side badge rendering is a Future work item in
  `doc/ui.md`. Until then, the title-prefix is the visible badge.
- **ydotool may be soft-passed.** See
  `todo/qdwin-vm/ydotool-install-uinput-missing.md`.
- **`qdistro-tier5-base.qcow2` is required.** Build-once,
  reuse-everywhere. The scenario ERRORs (not FAILs) if absent —
  building is a separate, multi-minute orchestrator-side step.
