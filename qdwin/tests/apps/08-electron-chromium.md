# 08 — Electron / Chromium browser via XWayland

<!-- qci:visual: required -->

**Acceptance criterion:** Chromium (representing the Electron browser
class) launches via XWayland, renders a real web page, survives a
maximise/restore cycle. The Wayland-Ozone path stays black under the
pixman renderer (tracked separately as the Krita/Obsidian/Kate
"all-black" cluster); this scenario verifies the XWayland fallback,
which is the load-bearing path for most Electron apps in practice.

## Setup

```bash
source ${QDWIN_REPO}/tests/apps/qdwin-apps-helpers.sh
qdwin_apps_set_vm "${VMNAME}"
qdwin_apps_session_up || { echo "FAIL: bystander/weston not healthy"; exit 1; }
if ! "$QDWIN_VM_EXEC" "$VMNAME" 'command -v chromium >/dev/null 2>&1'; then
    echo "SKIP: chromium not installed; qdwin app deps are opt-in (rerun with QDWIN_APP_DEPS=1)"
    exit 0
fi
qdwin_apps_kill_all
"$QDWIN_VM_EXEC" "$VMNAME" 'rm -rf /tmp/chr-scenario08'
```

## Steps

### Step 1 — launch

```bash
qdwin_apps_launch chromium "env OZONE_PLATFORM=x11 QT_QPA_PLATFORM=xcb GDK_BACKEND=x11 chromium --no-sandbox --user-data-dir=/tmp/chr-scenario08 --ozone-platform=x11"
sleep 18
qdwin_apps_screenshot /tmp/08-step1-launched.png
```

**Assert (1.1):** screenshot shows Chromium with a tab bar (at least
one tab — the openSUSE search default page or `chrome://newtab`),
URL bar visible, and rendered web content (text, links, search box —
not just a blank canvas).
**Assert (1.2):** bystander log shows `toplevel_added handle=<N>
... title="..Chromium" xwayland=1`. (chromium does have a wayland
ozone path, but our test launcher doesn't activate it.)
**Assert (1.3):** weston pid unchanged.

### Step 2 — maximise

```bash
qdwin_apps_ctl "maxlast"
sleep 2
qdwin_apps_screenshot /tmp/08-step2-max.png
```

**Assert (2.1):** chromium fills the screen.

### Step 3 — restore

```bash
qdwin_apps_ctl "restorelast"
sleep 2
qdwin_apps_screenshot /tmp/08-step3-restore.png
```

**Assert (3.1):** chromium back at launch size.

## Cleanup

```bash
qdwin_apps_kill_all
```

## Pass criteria

- All three screenshots show rendered Chromium (not blank).
- bystander tags chromium `xwayland=1`.

## Known failure modes

- **GPU process exits** — `/tmp/chromium.log` shows
  `Exiting GPU process due to errors during initialization` and the
  canvas stays black. That's the native Wayland Ozone path failing
  under pixman; tracked in
  . Forcing
  `--ozone-platform=wayland` in the launch command would reproduce
  the bug; this scenario deliberately omits it.
- **Test pollution** — leftover `/tmp/chr-scenario08` from a prior
  failed run forces chromium into "Restoring tabs?" UI which doesn't
  match the assertions. Setup should `rm -rf /tmp/chr-scenario08`
  before launch.
- **Missing Chromium** — the qdwin app matrix is optional. A default
  lean GUI VM may omit Chromium unless the golden was built with
  `QDWIN_APP_DEPS=1`; in that profile this scenario is a clean SKIP,
  not an XWayland/Ozone regression.
