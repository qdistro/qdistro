# 12 — wl_keyboard delivery to native Wayland and XWayland clients

<!-- qci:visual: required -->

**Acceptance criterion:** keystrokes injected as discrete QMP
`input-send-event` key transitions reach both a native-Wayland focused toplevel
and an XWayland focused toplevel and produce visible characters.
Tests qdwin's `set_keyboard_focus` path (the bystander uses the v14
form, see `test-client/qdwin-bystander.c`) plus libweston's
keymap and Xwayland keyboard hand-off.

## Setup

```bash
source ${QDWIN_REPO}/tests/apps/qdwin-apps-helpers.sh
qdwin_apps_set_vm "${VMNAME}"
qdwin_apps_session_up || { echo "FAIL: bystander/weston not healthy"; exit 1; }
qdwin_apps_kill_all
```

## Steps

### Step 1 — type into native-Wayland Firefox

```bash
qdwin_apps_launch firefox "firefox --no-remote --new-instance about:blank"
sleep 12
# Bystander already called set_keyboard_focus on toplevel_added; the
# URL bar gets focus by default in a fresh tab.
qdwin_apps_type "qdwin"
sleep 1
qdwin_apps_screenshot /tmp/12-step1-firefox-typed.png
```

**Assert (1.1):** screenshot shows `qdwin` typed into the Firefox URL
bar. Pre-fix, the held-layer-without-focus path would silently drop
keystrokes; this is the canary.

```bash
qdwin_apps_kill_all
sleep 1
```

### Step 2 — type into XWayland xterm

```bash
qdwin_apps_launch xterm "xterm"
sleep 4
qdwin_apps_type "echo qdwin"
qdwin_apps_send_key KEY_ENTER
sleep 1
qdwin_apps_screenshot /tmp/12-step2-xterm-typed.png
```

**Assert (2.1):** screenshot shows `echo qdwin` on one line and
`qdwin` (the shell output) on the next, with the prompt advanced to
a fresh line.

## Cleanup

```bash
qdwin_apps_kill_all
```

## Pass criteria

- Step 1: "qdwin" rendered in Firefox URL bar (Wayland keyboard).
- Step 2: command + output rendered in xterm (XWayland keyboard).

## Known failure modes

- **Step 1 URL bar empty** — Firefox didn't get keyboard focus. The
  bystander's `set_keyboard_focus` call at toplevel_added didn't take.
  Check bystander log for the call; check qdwin log for the
  `set_keyboard_focus` audit line.
- **Step 2 xterm shows only a blinking prompt** — XWayland's
  keyboard hand-off (xkb keymap forwarding) didn't happen. Check
  qdwin.log for "launching '/usr/bin/Xwayland'" and absence of
  immediate "exited with status" lines.
- **Chord problem** — if step 2 produces "Q" only and trails off,
  `qdwin_apps_type` is sending all keys as a chord rather than
  serially. Verify the helper inserts a sleep between
  `KEY_*` presses (the helper does this via `sleep 0.04`).
