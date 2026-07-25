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
$VMEXEC "$VM" 'runuser -u admin -- pgrep -af "[q]s -p" >/dev/null'

# Precondition: tier-2 image built and weston-terminal container can
# be spawned. s32 builds the image on first run; reuse if cached.
#
# The first-run image build is the expensive, UNBOUNDED step (270s+ on a
# cold cache) — prebuild + assert it HERE in Setup, bounded by an explicit
# timeout, so the expensive build is NOT on S1's critical path and cannot
# blow the scenario's wall-clock (rc=124) budget. A clean setup-failure
# message makes a slow/failed build obvious instead of an opaque timeout.
$VMEXEC "$VM" 'timeout 240 runuser -u admin -- bash -c "
    podman image exists qdistro/tier2-weston-terminal:latest \
        || bash /root/qdistro-src/qdistro/tier2/make-tier2-image.sh weston-terminal"' \
  || { echo "FAIL(setup): tier-2 image build/check exceeded 240s or failed"; exit 1; }
# Hard-assert the image now exists, so S1 only does the (fast) spawn+scan.
$VMEXEC "$VM" 'runuser -u admin -- podman image exists qdistro/tier2-weston-terminal:latest' \
  || { echo "FAIL(setup): qdistro/tier2-weston-terminal:latest missing after build"; exit 1; }

# Precondition: the broker must ALLOW the tier-2 spawn gate for this workload.
# Since `security: require broker allow for tier2 spawns`, spawn-tier2.sh fails
# closed (decision=unknown) unless a rule allows
# qdistro.tier2.spawn:<workload>/<app>. /etc/qdistro/rules.d is empty on a
# fresh VM, so author the allow rule here exactly like s40-tier2-hardening.sh /
# the silo-secctx-wiretag probe, reload the broker, and poll CheckPermission
# until it answers `allow` before S1 spawns. (S1 runs the workload as
# weston-terminal, so the action is weston-terminal/weston-terminal — the
# WORKLOAD/APP pair, not the tier2-c-ui container name.)
$VMEXEC "$VM" 'set -e
    RULE_DIR=/etc/qdistro/rules.d
    SPAWN_ACTION="qdistro.tier2.spawn:weston-terminal/weston-terminal"
    systemctl start qdistro-admin-broker.service 2>/dev/null || true
    install -d -m 0755 "$RULE_DIR"
    cat >"$RULE_DIR/zz-s18-podapps-allow.yaml" <<EOF
# Test-authored (permissions-gui/18): allow the tier-2 spawn of weston-terminal
# so the podapps-badge lane can mint the container.
- name: s18-podapps-weston-terminal-allow
  decision: allow
  match:
    action: $SPAWN_ACTION
EOF
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
    reply=""
    for _ in $(seq 1 20); do
        reply=$(runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
            dbus-send --system --print-reply=literal \
            --dest=org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 \
            org.qdistro.AdminBroker1.CheckPermission \
            "string:$SPAWN_ACTION" "dict:string:string:" 2>/dev/null | tr -d " \t\n")
        [ "$reply" = "allow" ] && break
        sleep 0.25
    done
    [ "$reply" = "allow" ]' \
  || { echo "FAIL(setup): broker did not load the tier-2 spawn allow rule for weston-terminal/weston-terminal"; exit 1; }

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

# Precondition: the input channel is ready. S2/S3 inject launcher keystrokes
# via vm-gui, which talks to ydotoold over /run/user/1000/ydotool.sock. If the
# daemon is down or the socket is missing, every keystroke is silently dropped
# and the launcher never opens — fail fast here rather than timing out.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_user_unit_active ydotoold.service admin' \
  || { echo "FAIL(setup): ydotoold not active / ydotool socket missing — launcher keystrokes would be dropped"; exit 1; }
$VMEXEC "$VM" 'test -S /run/user/1000/ydotool.sock' \
  || { echo "FAIL(setup): ydotoold not active / ydotool socket missing — launcher keystrokes would be dropped"; exit 1; }
```

## Steps

> **Budget discipline (this is a heavy tier-2 lane — stay inside the wall clock).**
> Only **one** screenshot requires visual judgment: `/tmp/s18-launcher-open.png`.
> Inspect it and decide hard pass criterion 4 (the tier-2 badge). The other hard
> criteria are deterministic shell checks (S1 `jq`) — run the command and read its
> exit status; do not "look" to decide them. The S4 `journalctl` grep is a
> diagnostic NOTE, not a pass criterion.
>
> The screenshots `/tmp/s18-launcher-empty.png`, `/tmp/s18-cold-start.png`, and
> `/tmp/s18-warm.png` are **artifact-only diagnostics**. You MUST still run the
> listed `vm-gui screenshot` commands so the PNG files exist (they are required
> triage artifacts), but you MUST NOT inspect, describe, compare, or reason about
> those images, and they MUST NOT affect PASS/FAIL.
>
> After Setup, S1, the apps.json `jq` assertion, the S2 launcher-open visual
> assertion (the tier-2 badge), the S3 launch action, and the S4 journal grep NOTE
> have completed, write
> `status.txt` immediately and STOP — do not continue exploring or collecting
> extra evidence. (S5 cleanup is best-effort and may be skipped: each scenario
> runs on a fresh disposable VM.) Spending vision round-trips on the
> artifact-only screenshots is what blows the wall-clock budget (rc=124) on an
> otherwise-passing scenario.

### S1 — populate the podapps cache for a not-yet-running container

The scan helper requires the container to be running, so spawn it
first in detached mode and run the scan in foreground.

The image is already built (Setup precondition), so this step is just the
fast spawn + foreground scan. Bound it with an explicit timeout so a
hung spawn/scan fails loudly instead of consuming the scenario's
wall-clock budget.

```bash
$VMEXEC "$VM" 'timeout 60 runuser -u admin -- bash -c "
    nohup env QDISTRO_PROFILE=dev TIER2_DETACH=1 \
        /root/qdistro-src/qdistro/tier2/spawn-tier2.sh \
        tier2-c-ui weston-terminal -- weston-terminal \
        >/tmp/qci-permissions-gui-18-podapps-spawn.log 2>&1 </dev/null &
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
$VMGUI "$VM" screenshot /tmp/s18-launcher-empty.png

# Open launcher via configured keybinding (qdwin emits launcher_requested
# → qdshell opens it). On the VM image this is Ctrl+Space by default.
# Inject through vm-gui (exports YDOTOOL_SOCKET + gates on ydotoold) like
# the rest of the GUI suite; bare ydotool drops keystrokes without it.
$VMGUI "$VM" key ctrl+space
sleep 1

# The combined launcher can contain more rows than fit in the viewport, and
# provider Component.onCompleted order is not a stable visibility contract.
# Filter by the unique container name so the screenshot proves the PodApps row
# itself instead of merely proving that the first page contains other providers.
$VMGUI "$VM" type "tier2-c-ui"
sleep 0.3

$VMGUI "$VM" screenshot /tmp/s18-launcher-open.png
```

**Assert** (hard, agent-visual): the launcher's app list contains at
least one `tier2-c-ui` PodApps entry, rendered with the tier-2 silo
signal the PodApps provider currently emits — the pink/magenta square
tier-2 **badge** on the entry (the delegate-side `modelData.badgeIcon`;
see qdshell `Modules/Panels/Launcher/Providers/PodAppsProvider.qml` +
`doc/containers.md`). This is the prominent, agent-readable signal; the
older `[tier2/tier2-c-ui]` description-line text is only a non-prominent
stand-in per `doc/containers.md` and is NOT what to look for.

If the badge is genuinely absent on a launcher entry that clearly maps to
the tier-2 weston-terminal app, fail. A framebuffer too dark to read any
launcher text is a capture problem, not a missing badge — re-screenshot
once; do not infer a missing badge from unreadable text.

### S3 — launch a podapps entry (diagnostic cold-start exercise)

The type+Enter here drives the launcher and captures the cold-start PNG as a
diagnostic artifact. It is NOT a hard assertion: `type "weston-terminal"`+Enter
selects the top filtered match, and on the tier-5-provisioned image a tier-5
`weston-terminal` provider entry can outrank the tier-2 PodApps one — so the
resulting toplevel is not deterministically the tier-2 container. The
launcher→tier-2-spawn journal contract is covered deterministically by
`tests/integration/vm/tiered-isolation.bats:phase7-tier2-podman`; here it is
an artifact-only exercise.

```bash
# Clear S2's unique PodApps filter, then type weston-terminal and press Enter.
$VMGUI "$VM" key ctrl+a
$VMGUI "$VM" key backspace
$VMGUI "$VM" type "weston-terminal"
sleep 0.3
$VMGUI "$VM" key enter

# Screenshot immediately — before the inner toplevel can map.
$VMGUI "$VM" screenshot /tmp/s18-cold-start.png
```

**Diagnostic artifact only** (do NOT analyze, do NOT affect the verdict):
`/tmp/s18-cold-start.png` records whether the taskbar (or dock) shows a new
weston-terminal entry at reduced opacity with a spinner overlay (the
cold-start contract in `doc/window-hierarchy.md`). Leave it as an artifact;
do not spend a vision round-trip on it and never fail on it.

### S4 — placeholder resolves on toplevel arrival

Wait up to 5s, then screenshot again.

```bash
sleep 5
$VMGUI "$VM" screenshot /tmp/s18-warm.png

# Cross-check journal for the matching secctx event (diagnostic NOTE only).
$VMEXEC "$VM" "journalctl --since '30s ago' | grep 'toplevel_security_context.*tier2-c-ui'" || true
```

**NOTE** (diagnostic, not a pass criterion): a matching
`toplevel_security_context.*tier2-c-ui` line corroborates the S3 launch reached
the tier-2 container, but S3's `type+Enter` is not a deterministic tier-2 launch
(see S3 — the tier-5/tier-2 `weston-terminal` name collision), so a miss here is
a NOTE, not a failure. The deterministic launcher→tier-2 journal contract lives
in `tiered-isolation.bats:phase7-tier2-podman`. This mirrors the journal-side
twin `tests/integration/vm/s60-launcher-podapp-click.sh`, which likewise treats a
missing post-Enter advertise as a NOTE.

Until tracker J12 this line was not merely non-deterministic here — it was
**unproducible in any topology**, because a tier-2 window arrives as a nested
proxy and qdwin skipped proxies in its `toplevel_security_context` sender. Both
halves of that (the un-tagged launch and the skipped proxy) are fixed, and the
deterministic proof — a real `LaunchPodApp` click whose secctx commit AND
nested-proxy forward both carry the token the D-Bus reply returned — lives in
`tests/integration/vm/podapp-launch-wiretag.bats`. The NOTE here stays a NOTE
for its own reason: S3's `type+Enter` cannot deterministically select the
tier-2 entry.

**Diagnostic artifact only** (do NOT analyze, do NOT affect the verdict):
`/tmp/s18-warm.png` records whether the taskbar entry is now at full opacity
with no spinner overlay. Leave it as an artifact for the S3/S4 cold-start
transition; do not spend a vision round-trip on it and never fail on it.

## Pass criteria

Hard pass conditions:

1. Setup preconditions pass, including the qdlocker-not-locked guard.
2. S1 populates `/var/lib/qdistro/podapps/tier2-c-ui/apps.json`.
3. `apps.json` contains a tier-2 marker (`"silo": "tier2/tier2-c-ui"`)
   and non-empty `execArgv`.
4. S2 visually confirms at least one tier-2 PodApps launcher entry bearing
   the pink/magenta square tier-2 **badge** (the signal the PodApps provider
   currently emits). **This is the only step that needs visual reasoning.**

S3's launch and the S4 `toplevel_security_context.*tier2-c-ui` journal line are
diagnostic NOTEs only (the type+Enter launch is non-deterministic across the
tier-5/tier-2 `weston-terminal` name collision; the journal contract is owned by
`tiered-isolation.bats:phase7-tier2-podman`). The S2/S3/S4 cold-start
screenshots are best-effort diagnostic artifacts only — capture them, do not
analyze them, never fail on them.

### S5 — cleanup

```bash
$VMEXEC "$VM" 'runuser -u admin -- podman stop -t 2 tier2-c-ui >/dev/null 2>&1 || true'
$VMEXEC "$VM" 'pkill -u admin -f "spawn-tier2.sh tier2-c-ui" 2>/dev/null || true'
# Remove the test-authored broker allow rule and reload, so subsequent
# scenarios start from the fail-closed default (mirrors the probe teardown).
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/zz-s18-podapps-allow.yaml 2>/dev/null || true
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true'
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
