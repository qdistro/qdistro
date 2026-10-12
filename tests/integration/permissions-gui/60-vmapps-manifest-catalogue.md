# 60 — the tier-5 launcher catalogue comes from the guest-base manifest

<!-- qci:visual: required -->

**What**: stage a vmapps manifest at
`/usr/share/qdistro/vmapps/base.json`, verify the launcher surfaces the
discovered tier-5 entries; delete the manifest and verify the catalogue
degrades to exactly the Foot entry; restore it, open the Settings
"Sandboxed VM apps" tab, and prove a per-app lifecycle override keyed on
the stable `tier5/konsole` appId lands in `tier5-lifecycle.conf`.

**Why**: `VMApps.qml` dropped its hardcoded catalogue for the manifest
`tier5-vm/vmapps-scan.py` publishes from the guest base (vmapps-dynamic).
The loader must (a) render discovered rows under their STABLE appIds —
the Settings/policy/broker keys — (b) degrade to exactly Foot when the
manifest is missing, unparseable, empty, or stale, and (c) reload live
on manifest changes so a rebuilt guest base republishes without a
session restart. This scenario stages manifests directly (the scanner's
output shape is covered host-side by `tests/unit/test_vmapps_scan.py`
and `tier5-vmapps-reconcile.bats`); here we prove the loader and its
downstream Settings plumbing.

## Setup

```bash
VM=${VMNAME:?set VMNAME to the target VM}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui
ART=${QCI_GUI_ARTIFACT_DIR:-/tmp}/60
mkdir -p "$ART"
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh
qdwin_set_vm "$VM"
```

Precondition: qdwin lane (product session owns `wayland-1`, qdshell up).

```bash
$VMEXEC "$VM" 'runuser -u admin -- test -S /run/user/1000/wayland-1' \
    || { echo "ERROR: no wayland-1 — not a qdwin-lane VM"; exit 2; }
$VMEXEC "$VM" 'runuser -u admin -- pgrep -af "[q]s -p" >/dev/null' \
    || { echo "ERROR: qdshell not running"; exit 2; }
```

Keep qdlocker from eating launcher keystrokes (same block as
scenario 18):

```bash
$VMEXEC "$VM" 'set -e
    faillock --user admin --reset 2>/dev/null || true
    install -d -m 0755 -o 0 -g 0 /etc/qdistro
    : > /etc/qdistro/locker-ctrl-introspection
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
```

Fixture manifest — the exact shape `vmapps-scan.py` publishes (schema 1,
stringified `execArgv`, stable appIds). Konsole exercises the legacy
alias (`org.kde.konsole` → `tier5/konsole`); foot is always present in a
real base. Staged by root at 0644, matching the sidecar install.

```bash
$VMEXEC "$VM" 'install -d -m 0755 /usr/share/qdistro/vmapps'
$VMEXEC "$VM" 'cat > /usr/share/qdistro/vmapps/base.json <<'"'"'EOF'"'"'
{
  "schema": 1,
  "scanner": "vmapps-scan.py 1",
  "generated": "2026-10-10T00:00:00Z",
  "apps": [
    {
      "appId": "tier5/konsole",
      "desktopId": "org.kde.konsole",
      "name": "Konsole",
      "iconName": "utilities-terminal",
      "comment": "KDE terminal (Qt6/KDE Frameworks, SSD)",
      "execArgv": "[\"konsole\"]"
    },
    {
      "appId": "tier5/firefox",
      "desktopId": "firefox",
      "name": "Firefox",
      "iconName": "firefox",
      "comment": "Isolated Firefox in a per-app VM",
      "execArgv": "[\"firefox\"]"
    },
    {
      "appId": "tier5/foot",
      "desktopId": "foot",
      "name": "Foot",
      "iconName": "utilities-terminal",
      "comment": "Isolated foot terminal in a per-app VM",
      "execArgv": "[\"foot\"]"
    }
  ]
}
EOF
    chmod 0644 /usr/share/qdistro/vmapps/base.json'
```

Wait for the loader to publish the swap — the journal line is the only
externally-visible evidence that the catalogue changed:

```bash
$VMEXEC "$VM" 'for i in $(seq 1 40); do
    runuser -u admin -- journalctl --user -u qdshell.service -n 60 --no-pager 2>/dev/null \
        | grep -q "manifest loaded: 3 app(s)" && exit 0
    sleep 0.5
  done; exit 1' \
  || { echo "FAIL(setup): qdshell did not log 'manifest loaded: 3 app(s)'"; exit 1; }
```

### S1 — the launcher lists the discovered entries

Open the launcher, filter for the discovered app, capture.

```bash
$VMGUI "$VM" key ctrl+space
sleep 1
$VMGUI "$VM" type "konsole"
sleep 1
qdwin_screenshot "$ART/s1-launcher-konsole.png"
```

**Assert** (hard, agent-visual): the launcher's app list shows a row for
**Konsole** marked as a VM app (`[VM]` description prefix per the
provider's convention). If Konsole is absent, check whether another
provider row consumed the filter, then FAIL — a staged manifest whose
rows never reach the provider is the defect this scenario exists to
catch.

Close the launcher before continuing:

```bash
$VMGUI "$VM" key Escape
sleep 1
```

### S2 — deleting the manifest drops the catalogue to Foot only

```bash
$VMEXEC "$VM" 'rm -f /usr/share/qdistro/vmapps/base.json'
$VMEXEC "$VM" 'for i in $(seq 1 40); do
    runuser -u admin -- journalctl --user -u qdshell.service -n 60 --no-pager 2>/dev/null \
        | grep -q "foot-only fallback" && exit 0
    sleep 0.5
  done; exit 1' \
  || { echo "FAIL(S2): qdshell never logged the foot-only fallback"; exit 1; }
```

Reopen the launcher and filter for foot — and confirm the discovered
apps are gone.

```bash
$VMGUI "$VM" key ctrl+space
sleep 1
$VMGUI "$VM" type "foot"
sleep 1
qdwin_screenshot "$ART/s2-launcher-foot.png"
$VMGUI "$VM" key ctrl+a
$VMGUI "$VM" key backspace
$VMGUI "$VM" type "konsole"
sleep 1
qdwin_screenshot "$ART/s2-launcher-konsole-gone.png"
```

**Assert** (hard, agent-visual): `s2-launcher-foot.png` shows **Foot
Terminal (VM)** as the only VM-catalogue row, and
`s2-launcher-konsole-gone.png` shows **no Konsole row**. Foot is the
designed fallback — a stale Konsole surviving a deleted manifest is the
regression.

```bash
$VMGUI "$VM" key Escape
sleep 1
```

### S3 — restored manifest reaches the Settings override surface

Re-stage the manifest (same heredoc as Setup), wait for the reload, and
open the Settings "Sandboxed VM apps" tab through qdshell's IPC:

```bash
$VMEXEC "$VM" 'cat > /usr/share/qdistro/vmapps/base.json <<'"'"'EOF'"'"'
{
  "schema": 1,
  "scanner": "vmapps-scan.py 1",
  "generated": "2026-10-10T00:00:00Z",
  "apps": [
    {
      "appId": "tier5/konsole",
      "desktopId": "org.kde.konsole",
      "name": "Konsole",
      "iconName": "utilities-terminal",
      "comment": "KDE terminal (Qt6/KDE Frameworks, SSD)",
      "execArgv": "[\"konsole\"]"
    },
    {
      "appId": "tier5/foot",
      "desktopId": "foot",
      "name": "Foot",
      "iconName": "utilities-terminal",
      "comment": "Isolated foot terminal in a per-app VM",
      "execArgv": "[\"foot\"]"
    }
  ]
}
EOF
    chmod 0644 /usr/share/qdistro/vmapps/base.json'
$VMEXEC "$VM" 'for i in $(seq 1 40); do
    runuser -u admin -- journalctl --user -u qdshell.service -n 60 --no-pager 2>/dev/null \
        | grep -q "manifest loaded: 2 app(s)" && exit 0
    sleep 0.5
  done; exit 1' \
  || { echo "FAIL(S3): qdshell did not reload the restored manifest"; exit 1; }

$VMEXEC "$VM" 'runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=wayland-1 \
    qs ipc -p /usr/share/quickshell/qdshell call settings openTab vmapps' \
  || { echo "FAIL(S3): could not open the vmapps settings tab via IPC"; exit 1; }
sleep 2
qdwin_screenshot "$ART/s3-vmapps-tab.png"
# The tab is taller than the 1280x800 test screen: the per-app rows sit
# at the bottom of its scroll area. Wheel over the tab body (QMP input)
# to bring the last row into view.
$VMGUI "$VM" scroll down 10 740 400
sleep 1
qdwin_screenshot "$ART/s3-vmapps-tab-bottom.png"
```

**Assert** (hard, agent-visual): `s3-vmapps-tab.png` shows the Settings
panel on the "Sandboxed VM apps" tab with a **Konsole** per-app override
row, and `s3-vmapps-tab-bottom.png` (the same tab scrolled down) shows
both a **Konsole** and a **Foot** row from the restored manifest — each
row carrying a per-app override control. If the bottom frame still ends
at Konsole with no Foot row below it, that is a FAIL. This
proves the Settings override surface is fed by the same discovered
catalogue (the appId is the policy key; a row the tab cannot see cannot
be overridden).

Toggle the Konsole row's override ON via the click-preview/click-confirm
handshake, then verify the policy file — the override is written keyed
on the stable appId:

```bash
# Model-targeted click on Konsole's per-app override control.
qdwin_screenshot "$ART/s3-pre-click.png"
# (agent: use vm-gui click-preview on the override control of the
#  "Konsole" row, verify the frame, then click-confirm)

$VMEXEC "$VM" 'for i in $(seq 1 20); do
    grep -q "\[app:tier5/konsole\]" /home/admin/.config/qdistro/tier5-lifecycle.conf 2>/dev/null \
        && exit 0
    sleep 0.5
  done; exit 1' \
  || { echo "FAIL(S3): override for tier5/konsole never landed in tier5-lifecycle.conf"; exit 1; }
qdwin_screenshot "$ART/s3-override-on.png"
```

**Assert** (hard, script): `tier5-lifecycle.conf` contains an
`[app:tier5/konsole]` section — the stable-appId contract end to end:
desktop-ID → scanner alias → manifest row → Settings policy key.

### S4 — activating the discovered row reaches spawn with its appId (journal)

No guest base exists in the GUI VM, so the spawn fails fast — but the
journal proves `VMApps.launch(row)` carried the discovered row's stable
appId all the way to the spawn helper (`--policy-key`). Open the
launcher, activate the Konsole row, then check qdshell's journal for the
spawn stderr prefix VMApps stamps with the appId:

```bash
$VMGUI "$VM" key ctrl+space
sleep 1
$VMGUI "$VM" type "konsole"
sleep 1
$VMGUI "$VM" key enter
sleep 1
$VMGUI "$VM" key Escape
$VMEXEC "$VM" 'for i in $(seq 1 60); do
    runuser -u admin -- journalctl --user -u qdshell.service -n 200 --no-pager 2>/dev/null \
        | grep -qE "spawn stderr \(tier5/konsole\)" && exit 0
    sleep 0.5
  done; exit 1' \
  || { echo "FAIL(S4): no 'spawn stderr (tier5/konsole)' journal line — launch plumbing did not carry the discovered appId"; exit 1; }
```

**Assert** (hard, journal): a `spawn stderr (tier5/konsole)` line — the
spawn helper ran and failed on the absent base, which is expected; the
prefix proves the discovered row's stable appId drove the launch. A
polkit prompt instead of a launch is a FAIL (the active admin session is
authorized by the shipped policy).

### S5 — teardown

```bash
$VMEXEC "$VM" 'rm -f /usr/share/qdistro/vmapps/base.json
    rm -f /home/admin/.config/qdistro/tier5-lifecycle.conf
    runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
        qs ipc -p /usr/share/quickshell/qdshell call settings toggle 2>/dev/null || true'
$VMGUI "$VM" key Escape
```

## Report

- S1 launcher filter shows the discovered Konsole row (graded visual)
- S2 deleted manifest → Foot Terminal (VM) only; no Konsole (graded visual)
- S3 restored manifest reaches the Settings tab; the `tier5/konsole`
  override lands in `tier5-lifecycle.conf` (graded visual + file)
- S4 launcher activation reaches spawn under the stable appId (journal)
- Artifacts under `$ART/`: s1-launcher-konsole.png,
  s2-launcher-foot.png, s2-launcher-konsole-gone.png, s3-vmapps-tab.png,
  s3-vmapps-tab-bottom.png,
  s3-pre-click.png, s3-override-on.png
