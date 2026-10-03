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
source "$QDISTRO_REPO/qdwin/tests/apps/qdwin-apps-helpers.sh"
qdwin_apps_set_vm "${VMNAME}"
qdwin_apps_session_up || { echo "FAIL: bystander/weston not healthy"; exit 1; }
qdwin_apps_kill_all
# This host-side helper runs command -v inside VMNAME. The host's PATH is
# unrelated to the GUI golden and must not be used for this prerequisite.
qdwin_apps_guest_has_command foot || {
  echo "ERROR: foot, a core test client, is missing from the GUI golden"; exit 2;
}
qdwin_apps_guest_has_command xterm || {
  echo "ERROR: xterm, a core test client, is missing from the GUI golden"; exit 2;
}
```

## Steps

### Step 1 — type into native-Wayland foot

```bash
qdwin_apps_launch foot "foot"
sleep 4
# The bystander focuses each new toplevel. Type a command into foot's
# shell, then execute it so both the input and resulting output are visible.
qdwin_apps_type "echo qdwin"
qdwin_apps_send_key KEY_ENTER
sleep 1
qdwin_apps_screenshot "${QCI_GUI_ARTIFACT_DIR}/12-step1-foot-typed.png"
```

**Assert (1.1):** the screenshot shows `echo qdwin` and its `qdwin`
output in foot. The bystander log must also show foot's toplevel with
`xwayland=0`. Pre-fix, the held-layer-without-focus path silently dropped
keystrokes; foot exercises the native-Wayland keyboard path without relying
on optional Firefox app dependencies.

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
qdwin_apps_screenshot "${QCI_GUI_ARTIFACT_DIR}/12-step2-xterm-typed.png"
```

**Assert (2.1):** screenshot shows `echo qdwin` on one line and
`qdwin` (the shell output) on the next, with the prompt advanced to
a fresh line.
The bystander log must show xterm's toplevel with `xwayland=1`.

## Cleanup

```bash
qdwin_apps_kill_all
```

## Pass criteria

- Step 1: `echo qdwin` and its output rendered in foot (native-Wayland keyboard).
- Step 2: command + output rendered in xterm (XWayland keyboard).
- foot is tagged `xwayland=0`; xterm is tagged `xwayland=1`.

## Known failure modes

- **Step 1 command or output missing** — foot did not get keyboard focus
  or the native-Wayland key path failed. Check the bystander's focus call
  and qdwin's `set_keyboard_focus` audit line.
- **Step 2 xterm shows only a blinking prompt** — XWayland's
  keyboard hand-off (xkb keymap forwarding) didn't happen. Check
  qdwin.log for "launching '/usr/bin/Xwayland'" and absence of
  immediate "exited with status" lines.
- **Chord problem** — if step 2 produces "Q" only and trails off,
  `qdwin_apps_type` is sending all keys as a chord rather than
  serially. Verify the helper inserts a sleep between
  `KEY_*` presses (the helper does this via `sleep 0.04`).
