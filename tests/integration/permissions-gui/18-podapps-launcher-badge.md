# 18 — tier-2 podapps land in the launcher and survive the cold-start UX

**What**: scan a running `qdistro/tier2-weston-terminal` container,
verify the launcher shows the workload's app entries from the
PodApps provider, click one, verify a placeholder taskbar entry
appears with the BusyIndicator overlay, then verify the placeholder
is replaced by a real toplevel once `qdwin_shell_v1.toplevel_security_context`
arrives.

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

# Defensive (NOT the proven cause): disable the idle-lock for this VM's
# session so a long agent-driven scenario can't trip qdlocker mid-run.
# Codex hypothesised an idle-lock fired during S1, but no artifact
# evidence supports it — this is belt-and-suspenders only.
$VMEXEC "$VM" 'runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
    systemctl --user set-environment QDLOCKER_IDLE_MS=0 2>/dev/null || true'
$VMEXEC "$VM" 'faillock --user admin --reset 2>/dev/null || true'
case "$($VMEXEC "$VM" 'runuser -u admin -- bash -c "printf \"status\n\" | socat -t 1 - UNIX-CONNECT:/run/user/1000/qdlocker.sock 2>/dev/null"' 2>/dev/null)" in
    *locked=True*)
        echo "FAIL(setup): qdlocker is already engaged; launcher input would be captured by the lock overlay"
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

Must include `"silo": "tier2/tier2-c-ui"` and a non-empty
`"execArgv"`.

### S2 — open the launcher; podapps entries visible

```bash
$VMGUI screenshot "$VM" /tmp/s18-launcher-empty.png

# Open launcher via configured keybinding (qdwin emits launcher_requested
# → qdshell opens it). On the VM image this is Ctrl+Space by default.
$VMEXEC "$VM" 'runuser -u admin -- ydotool key ctrl+space || true'
sleep 1

$VMGUI screenshot "$VM" /tmp/s18-launcher-open.png
```

**Assert** (agent-visual): the launcher's app list contains at least
one entry whose description begins with `[tier2/tier2-c-ui]`. That
prefix is the placeholder for the silo badge convention from
`doc/ui.md`; once delegate-side badge rendering lands, this assertion
shifts to "icon has the badge overlay".

### S3 — click a podapps entry; placeholder taskbar appears

```bash
# Type to filter to weston-terminal, then Enter.
$VMEXEC "$VM" 'runuser -u admin -- ydotool type "weston-terminal"'
sleep 0.3
$VMEXEC "$VM" 'runuser -u admin -- ydotool key enter'

# Screenshot immediately — before the inner toplevel can map.
$VMGUI screenshot "$VM" /tmp/s18-cold-start.png
```

**Assert** (agent-visual): the taskbar (or dock) shows a new entry
for weston-terminal with reduced opacity and a small spinner overlay.
Per the cold-start contract in `doc/window-hierarchy.md`, the entry
should NOT yet match a real toplevel.

### S4 — placeholder resolves on toplevel arrival

Wait up to 5s, then screenshot again.

```bash
sleep 5
$VMGUI screenshot "$VM" /tmp/s18-warm.png

# Cross-check journal for the matching secctx event.
$VMEXEC "$VM" "journalctl --since '30s ago' | grep 'toplevel_security_context.*tier2-c-ui'"
```

**Assert** (agent-visual): the taskbar entry is now at full opacity,
no spinner overlay, and renders normally. The journal grep must
produce at least one matching line (load-bearing assertion per the
repo convention).

### S5 — cleanup

```bash
$VMEXEC "$VM" 'podman stop -t 2 tier2-c-ui >/dev/null 2>&1 || true'
$VMEXEC "$VM" 'pkill -u admin -f "spawn-tier2.sh tier2-c-ui" 2>/dev/null || true'
```

## Known caveats

- **No delegate-side badge** yet (tracked in `doc/containers.md`
  "Future work"). The visual signal during S2 / S4 is the `[tier2/<name>]`
  description prefix.
- ydotool is the assumed input injector. The VM image bakes it in;
  see `scripts/vm/install-deps.sh`. If ydotool ever moves to wtype or
  similar, update S2/S3 here.
