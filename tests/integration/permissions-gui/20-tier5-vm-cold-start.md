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
VM=${VMNAME:?set VMNAME to the target VM (these scenarios are driven with an explicit VM)}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

# Precondition: outer compositor + qdshell up.
$VMEXEC "$VM" 'runuser -u admin -- test -S /run/user/1000/wayland-1'
$VMEXEC "$VM" 'runuser -u admin -- pgrep -af "[q]s -p" >/dev/null'

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
# TIER5_MEM_KIB=2097152 (2 GiB): the former 512 MiB CI accommodation was too
# tight for the guest kernel + compositor + app stack and could kill the guest
# mid-boot. Keep the scenario explicit so CI and the bats probes use one budget.
TIER5_MEM_KIB=2097152 setsid bash /tmp/qdistro-tier5/tier5-vm/spawn-tier5.sh --vm "$VM5" \
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

`virsh domstate` is NOT a reliable liveness oracle for a tier-5 nested
VM: under nested virt libvirt's `domstate` **lies**, reporting `shut
off` while the backing qemu process is still ALIVE (this is the exact
failure mode `tier5-vm/spawn-tier5.sh`'s `qemu_pids_for_domain` helper
and commit `8ac0a39` were written for). So `domstate` is a DIAGNOSTIC
here, not the verdict. The domain is **live** if `domstate=running` OR a
real qemu process backs `guest=$VM5` AND either libvirt `guest-ping`
answers OR `spawn-tier5.sh` has logged its own `[tier5] qga ready` /
`[tier5] guest publisher pid=` proof. It is only genuinely DEAD (a real
nested-VM death) when no backing qemu pid exists AND none of those
liveness proofs appears within the 90s budget.

```bash
# Run the whole liveness determination IN the outer VM (base64 to avoid
# nested-quoting hell, like S1). Reuses spawn-tier5.sh's robust check:
# pgrep -f "guest=$VM5," narrowed to real qemu by /proc/<pid>/comm.
B64=$(base64 -w0 <<EOF
VM5=$VM5
deadline=\$((SECONDS + 90))
verdict=DEAD; via=""; qpids=""
while [ \$SECONDS -lt \$deadline ]; do
    state=\$(runuser -u admin -- virsh domstate "\$VM5" 2>/dev/null | tr -d '[:space:]')
    qpids=""
    for p in \$(runuser -u admin -- pgrep -f "[g]uest=\$VM5," 2>/dev/null); do
        c=\$(cat "/proc/\$p/comm" 2>/dev/null) || continue
        case "\$c" in qemu-system-*|qemu-kvm) qpids="\$qpids \$p" ;; esac
    done
    qga=\$(runuser -u admin -- virsh qemu-agent-command --timeout 5 "\$VM5" '{"execute":"guest-ping"}' 2>/dev/null)
    spawn_live=0
    grep -Eq '\\[tier5\\] (qga ready|guest publisher pid=)' /tmp/s20-spawn.log 2>/dev/null && spawn_live=1
    if [ "\$state" = running ]; then verdict=LIVE; via="domstate=running"; break; fi
    if [ -n "\$qpids" ] && printf '%s' "\$qga" | grep -q '"return"'; then
        verdict=LIVE; via="qemu-pid+qga (domstate=\${state:-empty} LIES)"; break
    fi
    if [ -n "\$qpids" ] && [ "\$spawn_live" = 1 ]; then
        verdict=LIVE; via="qemu-pid+spawn-tier5-log (domstate=\${state:-empty} LIES)"; break
    fi
    sleep 2
done
echo "S2_VERDICT=\$verdict"
echo "S2_VIA=\$via"
echo "--- diagnostics ---"
echo "domstate: \$(runuser -u admin -- virsh domstate "\$VM5" 2>&1)"
echo "qemu pids:\${qpids:- <none>}"
for p in \$qpids; do echo "cmdline[\$p]: \$(tr '\0' ' ' </proc/\$p/cmdline 2>/dev/null)"; done
echo "guest-ping: \$(runuser -u admin -- virsh qemu-agent-command --timeout 5 "\$VM5" '{"execute":"guest-ping"}' 2>&1)"
tail -20 /tmp/s20-spawn.log 2>/dev/null
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash" 2>&1 | tee "${QCI_SCENARIO_TMPDIR:-/tmp}/s20-liveness.log"
```

**Assert**: the block prints `S2_VERDICT=LIVE` — the domain either
reached `domstate=running` OR is provably alive via a real backing qemu
pid plus `guest-ping` or `spawn-tier5.sh`'s own qga-ready/publisher log
even though `domstate` lied `shut off` (the `S2_VIA` line records
which). **FAIL only** on `S2_VERDICT=DEAD` — NO accepted liveness proof
within 90s: neither `domstate=running` nor a real qemu pid paired with
guest-agent/publisher evidence (a guest that never came up, OOM/panic'd
so qemu exited, or is only a wedged qemu with no guest-side progress).
Attach `${QCI_SCENARIO_TMPDIR:-/tmp}/s20-liveness.log` + the spawn log. A `domstate=shut off`
reading is NOT by itself a failure when qemu+qga prove the guest is
alive. (S2 asserts LIVENESS only; S3 separately proves the publisher
handshake and the visible toplevel.)

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
window. If a title bar or taskbar entry is visible, prefer seeing the
title-prefix `[tier5:$VM5]`, but do not fail solely because the current
terminal chrome renders only `Wayland Terminal`; S4 is the load-bearing
input/path proof.

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

### S5 — journal cross-check (soft / diagnostic)

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
