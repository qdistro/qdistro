# 18 — tier-2 podapps land in the launcher and survive the cold-start UX

**What**: scan a running `qdistro/tier2-weston-terminal` container,
verify the launcher shows the workload's app entries from the
PodApps provider with the current tier-2 visual signal: the
pink/magenta square badge. Also verify the PodApps cache carries the
tier-2 silo marker and non-empty `execArgv`. The delegate-side overlay
and S3/S4 cold-start visual transition are diagnostic until that
rendering path is merged and stable.

**Why**: this is the pixel-level half of the tier-2 cold-start
contract. The journal side (advertise + secctx arrival) is covered
by `tests/integration/vm/tiered-isolation.bats:phase7-tier2-podman`.
Here we corroborate visually that the qdshell side renders the
right thing at the right time.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-0052}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

# Precondition: outer compositor + qdshell up.
$VMEXEC "$VM" 'runuser -u admin -- test -S /run/user/1000/wayland-1'
$VMEXEC "$VM" 'runuser -u admin -- pgrep -af 'qs -p' >/dev/null'

# Precondition: tier-2 image built and weston-terminal container can
# be spawned. s32 builds the image on first run; reuse if cached.
#
# The first-run image build is the expensive, UNBOUNDED step (270s+ on a
# cold cache) — prebuild + assert it HERE in Setup, bounded by an explicit
# timeout, so the expensive build is NOT on S1's critical path and cannot
# blow the scenario's wall-clock (rc=124) budget. A clean setup-failure
# message makes a slow/failed build obvious instead of an opaque timeout.
$VMEXEC "$VM" 'timeout 240 bash -c "
    podman image exists qdistro/tier2-weston-terminal:latest \
        || bash /root/qdistro-src/qdistro/tier2/make-tier2-image.sh weston-terminal"' \
  || { echo "FAIL(setup): tier-2 image build/check exceeded 240s or failed"; exit 1; }
# Hard-assert the image now exists, so S1 only does the (fast) spawn+scan.
$VMEXEC "$VM" 'podman image exists qdistro/tier2-weston-terminal:latest' \
  || { echo "FAIL(setup): qdistro/tier2-weston-terminal:latest missing after build"; exit 1; }

# Keep qdlocker from stealing launcher keystrokes. The CI artifact from
# 2026-06-21 showed the round-2 set-environment-only guard was too weak:
# the already-running locker stayed locked and consumed "weston-terminal"
# as a password. Install the same test-lane drop-in used by qdlocker GUI
# helpers, enable ctrl introspection, restart the unit so the new idle
# timeout actually takes effect, and fail closed if the lock is still up.
$VMEXEC "$VM" 'set -e
    faillock --user admin --reset 2>/dev/null || true
    install -d -m 0755 -o 0 -g 0 /etc/qdistro
    : > /etc/qdistro/locker-ctrl-introspection
    chown 0:0 /etc/qdistro/locker-ctrl-introspection
    chmod 0644 /etc/qdistro/locker-ctrl-introspection
    install -d -m 0755 -o admin -g users /home/admin/.config/systemd/user/qdlocker.service.d
    cat >/home/admin/.config/systemd/user/qdlocker.service.d/90-ci-gui.conf <<EOF
[Service]
Environment=QDLOCKER_IDLE_MS=86400000
EOF
    chown admin:users /home/admin/.config/systemd/user/qdlocker.service.d/90-ci-gui.conf
    runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user daemon-reload
    runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart qdlocker.service
    sleep 3'
case "$($VMEXEC "$VM" 'runuser -u admin -- bash -c "printf \"status\n\" | socat -t 1 - UNIX-CONNECT:/run/user/1000/qdlocker.sock 2>/dev/null"' 2>/dev/null)" in
    *locked=False*) ;;
    *locked=True*)
        echo "FAIL(setup): qdlocker is locked; launcher input would be captured by the lock overlay"
        exit 1
        ;;
    *)
        echo "FAIL(setup): qdlocker status introspection unavailable; cannot prove launcher input is unobstructed"
        exit 1
        ;;
esac
```

## Steps

### S1 — populate the podapps cache for a not-yet-running container

The scan helper requires the container to be running, so spawn it
first in detached mode and run the scan in foreground.

The image is already built (Setup precondition), so this step is just the
fast spawn + foreground scan. Bound it with an explicit timeout so a
hung spawn/scan fails loudly instead of consuming the scenario's
wall-clock budget.

```bash
$VMEXEC "$VM" 'timeout 60 runuser -u admin -- bash -c "
    TIER2_DETACH=1 /root/qdistro-src/qdistro/tier2/spawn-tier2.sh \
        tier2-c-ui weston-terminal -- weston-terminal &
    sleep 3
    /root/qdistro-src/qdistro/tier2/podapps-scan.sh tier2-c-ui"' \
  || { echo "FAIL(S1): spawn+scan exceeded 60s or failed"; exit 1; }
```

**Expected** (stderr): `podapps-scan: tier2-c-ui → N entries`
(N ≥ 1; the weston-terminal image ships at least one .desktop file).

**Verify cache**:

```bash
$VMEXEC "$VM" 'cat /var/lib/qdistro/podapps/tier2-c-ui/apps.json | jq .[0]'
```

Hard assertion: `apps.json` must contain at least one entry with the
tier-2 badge marker (`"silo": "tier2/tier2-c-ui"`) and a non-empty
`"execArgv"`:

```bash
$VMEXEC "$VM" 'jq -e '"'"'any(.[]; .silo == "tier2/tier2-c-ui" and (.execArgv | type == "array" and length > 0))'"'"' /var/lib/qdistro/podapps/tier2-c-ui/apps.json'
```

### S2 — open the launcher; podapps entries visible

```bash
$VMGUI screenshot "$VM" /tmp/s18-launcher-empty.png

# Open launcher via configured keybinding (qdwin emits launcher_requested
# → qdshell opens it). On the VM image this is Ctrl+Space by default.
$VMEXEC "$VM" 'runuser -u admin -- ydotool key ctrl+space || true'
sleep 1

$VMGUI screenshot "$VM" /tmp/s18-launcher-open.png
```

**Assert** (hard, agent-visual): the launcher's app list contains at
least one `tier2-c-ui` PodApps entry and renders the current tier-2
visual signal: a pink/magenta square badge on the entry.

**Assert** (best-effort diagnostic): record whether the not-yet-merged
delegate-side badge overlay is visible. Do not fail the scenario on
the overlay check until the delegate rendering has landed.

TODO: re-tighten this assertion to require the delegate-side silo badge
overlay from `doc/ui.md` / `doc/containers.md` once that rendering path
merges.

### S3 — click a podapps entry; placeholder taskbar appears

```bash
# Type to filter to weston-terminal, then Enter.
$VMEXEC "$VM" 'runuser -u admin -- ydotool type "weston-terminal"'
sleep 0.3
$VMEXEC "$VM" 'runuser -u admin -- ydotool key enter'

# Screenshot immediately — before the inner toplevel can map.
$VMGUI screenshot "$VM" /tmp/s18-cold-start.png
```

**Assert** (best-effort diagnostic, agent-visual): record whether the
taskbar (or dock) shows a new entry for weston-terminal with reduced
opacity and a small spinner overlay. Per the cold-start contract in
`doc/window-hierarchy.md`, the entry should NOT yet match a real
toplevel, but this visual cold-start check must not fail the scenario.

### S4 — placeholder resolves on toplevel arrival

Wait up to 5s, then screenshot again.

```bash
sleep 5
$VMGUI screenshot "$VM" /tmp/s18-warm.png

# Cross-check journal for the matching secctx event.
$VMEXEC "$VM" "journalctl --since '30s ago' | grep 'toplevel_security_context.*tier2-c-ui'"
```

**Assert** (hard): the journal grep must produce at least one matching
`toplevel_security_context.*tier2-c-ui` line.

**Assert** (best-effort diagnostic, agent-visual): record whether the
taskbar entry is now at full opacity, has no spinner overlay, and
renders normally. Do not fail the scenario on this S3/S4 visual
cold-start transition.

## Pass criteria

Hard pass conditions:

1. Setup preconditions pass, including the qdlocker-not-locked guard.
2. S1 populates `/var/lib/qdistro/podapps/tier2-c-ui/apps.json`.
3. `apps.json` contains a tier-2 marker (`"silo": "tier2/tier2-c-ui"`)
   and non-empty `execArgv`.
4. S2 visually confirms at least one tier-2 PodApps launcher entry with
   the current pink/magenta square badge.
5. S4 journal output confirms `toplevel_security_context.*tier2-c-ui`.

The delegate-side badge overlay and S3/S4 visual cold-start transition
are best-effort diagnostics only.

### S5 — cleanup

```bash
$VMEXEC "$VM" 'podman stop -t 2 tier2-c-ui >/dev/null 2>&1 || true'
$VMEXEC "$VM" 'pkill -u admin -f "spawn-tier2.sh tier2-c-ui" 2>/dev/null || true'
```

## Known caveats

- **Delegate-side overlay re-tighten pending.** The hard visual signal
  during S2 is currently the pink/magenta square tier-2 badge. The
  not-yet-merged delegate-side overlay is diagnostic only; re-tighten
  the scenario once the rendering path in `doc/containers.md` "Future
  work" lands.
- ydotool is the assumed input injector. The VM image bakes it in;
  see `scripts/vm/install-deps.sh`. If ydotool ever moves to wtype or
  similar, update S2/S3 here.
