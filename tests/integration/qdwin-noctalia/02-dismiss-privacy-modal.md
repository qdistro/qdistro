# 02 — dismiss the first-run "Privacy Update" modal

**Acceptance criterion:** Noctalia's first-launch "Privacy Update"
consent modal can be dismissed via a left-click on its "GOT IT"
button, after which the wallpaper is fully visible.

This exercises:
- Layer-surface modal rendering (Noctalia models the modal as a
 dimmer + content layer surface, not an xdg_popup)
- Pointer event delivery to a layer surface
- Layer-surface destroy path on dismissal

This is the simplest interaction test that proves clicks reach
Noctalia.

## Setup

```bash
source qdwin/tests/gui/qdwin-helpers.sh
source tests/integration/qdwin-noctalia/noctalia-helpers.sh
qdwin_set_vm "${VMNAME:-noctalia-vis-260503-1021}"

# The "Privacy Update" telemetry wizard is shown on a Noctalia
# *upgrade* (UpdateService.qml triggers it when
# `compareVersions(changelogLastSeenVersion, "4.0.2") < 0` AND
# `Settings.isFreshInstall == false`). A wholly fresh user account
# would show the 5-step *first-run* setup wizard instead, which
# this scenario isn't asserting on. So: reset state, then seed
# settings.json to look like a pre-4.0.2 install — that is the
# minimum state required to trigger the privacy wizard.
ADMIN_USER=$("$QDWIN_VM_EXEC" "$VMNAME" 'getent passwd 1000 | cut -d: -f1' 2>/dev/null | tail -1)
"$QDWIN_VM_EXEC" "$VMNAME" "
 runuser -u $ADMIN_USER -- rm -rf /home/$ADMIN_USER/.config/noctalia /run/user/1000/quickshell
 install -d -o $ADMIN_USER -g $ADMIN_USER /home/$ADMIN_USER/.config/noctalia
 cat > /home/$ADMIN_USER/.config/noctalia/settings.json <<'JSON'
{
 \"application\": { \"isFreshInstall\": false },
 \"changelog\": { \"lastSeenVersion\": \"3.0.0\" }
}
JSON
 chown $ADMIN_USER:$ADMIN_USER /home/$ADMIN_USER/.config/noctalia/settings.json
"
noct_restart
# Wizard rendering is async; give it ~2s to mount.
sleep 2
```

## Steps

### Step 1 — capture initial frame, confirm modal is up

```bash
noct_screenshot_awake /tmp/02-step1-modal.png
```

**Assert (1.1):** the screenshot shows a dark dimmer covering the
desktop (background outside the modal is dimmed dark blue/black).
**Assert (1.2):** there is a centered modal panel with text
including "PRIVACY" or "ANONYMOUS" (OCR check: extract text from
the central 50% of the screenshot via tesseract; expect at least
one of those substrings).
**Assert (1.3):** there is a "GOT IT" button visible roughly
between (1100, 540) and (1230, 600).

### Step 2 — click "GOT IT"

```bash
qdwin_click 1170 570 left
sleep 1.5
noct_screenshot_awake /tmp/02-step2-after-click.png
```

**Assert (2.1):** the modal is gone — comparing the central 50% of
the screenshot to step 1, the dark dimmer is no longer present.
A pixel-level cheap check: average brightness of the central
1000×600 px is now > 40 (was < 30 with the modal up); the dimmer
is fully transparent or destroyed.
**Assert (2.2):** the wallpaper (Noctalia owl/moon or user's
configured wallpaper) is now visible in the central area.
**Assert (2.3):** the bar is still in the top 31 px.

### Step 3 — confirm the dimmer layer-surface was destroyed

```bash
"$QDWIN_VM_EXEC" "$VMNAME" \
 "runuser -l admin -c \"journalctl --user -u noctalia-session.service --since '30 seconds ago' --no-pager\"" \
 > /tmp/02-weston.log
```

**Assert (3.1):** zero protocol errors in the captured log.
**Assert (3.2):** Noctalia is still alive — `noct_session_healthy`.

## Cleanup

Modal stays dismissed across this VM's lifetime. To reset for next
test runs, delete `/home/admin/.config/noctalia/Settings.json`.

## Pass criteria

All asserts in steps 1-3 pass.

## Known failure modes

1. **Modal at unexpected coordinates** — Noctalia may center the
 modal at slightly different positions on different scaling
 factors. If the click in step 2 misses (assert 2.1 fails),
 re-screenshot, locate "GOT IT" via OCR with a tighter
 bounding-box, recompute the click target.

2. **Wallpaper not yet loaded** — the "Wallpaper Scan failed for
 Virtual-1 exit code: 1 (directory might not exist)" warning
 means `/home/admin/Pictures/Wallpapers` is empty. Default
 bundled wallpaper should still render. If post-dismiss
 screenshot is fully black, the wallpaper service has the issue.
 Not a layer-shell bug.
