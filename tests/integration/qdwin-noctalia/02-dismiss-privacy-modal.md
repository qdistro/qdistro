# 02 — no upgrade/privacy wizard on fresh launch

**Acceptance criterion:** A first-launch qdshell session shows the bar
and wallpaper immediately, with no telemetry / privacy / upgrade
modal in the way.

This is a regression test for the qdshell fork's strip pass.
Upstream Noctalia ships an `UpdateService.qml` that pops a
"Privacy Update" consent modal on version bumps, and a 5-step
first-run setup wizard for new installs. Both were removed during
the fork (see qdshell's CREDITS.md + the fork-base strip commit).
This scenario keeps them removed: if either component creeps back
in via an upstream cherry-pick, this test catches it.

## Setup

```bash
source qdwin/tests/gui/qdwin-helpers.sh
source tests/integration/qdwin-noctalia/noctalia-helpers.sh
qdwin_set_vm "${VMNAME:?set VMNAME to a running qdshell-on-qdwin VM}"

# Reset state so the first-run wizard *would* trigger if it still
# existed. Both ~/.config/qdshell/ (the qdshell-renamed settings
# path) and ~/.config/noctalia/ (the original) are cleared, so
# either codepath would fire.
ADMIN_USER=$("$QDWIN_VM_EXEC" "$VMNAME" 'getent passwd 1000 | cut -d: -f1' 2>/dev/null | tail -1)
"$QDWIN_VM_EXEC" "$VMNAME" "
    runuser -u $ADMIN_USER -- rm -rf \
        /home/$ADMIN_USER/.config/qdshell \
        /home/$ADMIN_USER/.config/noctalia \
        /run/user/1000/quickshell
"
noct_restart
sleep 2
```

## Steps

### Step 1 — fresh-launch frame

```bash
noct_screenshot_awake /tmp/02-fresh-launch.png
```

**Assert (1.1):** the bar is visible at the top of the screen.
**Assert (1.2):** the wallpaper (or default solid colour) is
visible across the full desktop — no centered modal panel, no dark
dimmer.
**Assert (1.3):** OCR of the central 50% of the screenshot contains
NEITHER of: "Privacy", "PRIVACY", "GOT IT", "Welcome to Noctalia",
"Setup Wizard", "Continue".

### Step 2 — service log

```bash
"$QDWIN_VM_EXEC" "$VMNAME" \
    "runuser -l admin -c \"journalctl --user -u noctalia-shell.service --since '30 seconds ago' --no-pager\"" \
    > /tmp/02-shell.log
```

**Assert (2.1):** zero protocol errors in the captured log.
**Assert (2.2):** `noct_session_healthy` returns OK — the shell
unit is active and `/usr/bin/qs` is running.
**Assert (2.3):** the log does NOT contain the strings
`UpdateService.qml`, `TelemetryWizard`, or `PrivacyModal` — those
modules were stripped and should not be re-introduced.

## Pass criteria

All asserts in steps 1 and 2 pass.

## Known failure modes

1. **Upstream cherry-pick re-introduced the wizard.** If a
   pull from noctalia-shell-upstream landed `UpdateService.qml` or
   `Modules/Welcome/`, the wizard fires again. The fix is to
   re-apply the strip to those files. See qdshell's CREDITS.md
   for the strip rationale; the source files to look at are
   `Commons/Settings.qml` (which gates the welcome flow) and any
   new `Modules/Welcome*/` directory.

2. **Bar not yet rendered** (assert 1.1 fails): `noctalia-shell.service`
   needs ~2-3 seconds after `noctalia-session.service` to bind its
   layer-shell surfaces. Increase the post-`noct_restart` sleep.
