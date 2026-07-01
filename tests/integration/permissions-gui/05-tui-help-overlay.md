# 05 — TUI `?` opens help overlay; any key dismisses

**What**: in the TUI running in qterminal, press `?` and verify the
help overlay (a modal over the main view) renders with the expected
text blocks; press Escape and verify the main view returns intact.

**Why**: the help overlay is the only place the full scope vocabulary
(`1`..`8`) and non-obvious keys (`Ctrl+P` palette, `r` refresh) are
documented to the user at runtime. A regression where `?` opens a
blank/broken modal silently reduces discoverability without breaking
any functional test.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-0052}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh && await_system_unit_active qdistro-admin-broker.service'
$VMEXEC "$VM" 'pkill -u admin qterminal 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_tui 2>/dev/null; true'
sleep 1
```

## Steps

### S1 — launch TUI, baseline screenshot

```bash
# Repo-supplied launcher; see scenarios 01/02 for the D-Bus-session
# env reason.
$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-tui'
sleep 3
$VMGUI "$VM" screenshot /tmp/05-tui-help-overlay-s1-main.png
```

**Assert (main view):**
- TUI header reads `qdistro admin approvals (TUI)` with subtitle
 containing `scope: Just this once`.
- Left pane shows the `uid action exe` table header (content may be
 empty — scenario does not inject a request).
- Right pane shows `(no request selected)`.
- No modal / overlay is on top of the main view.

### S2 — press `?`, help overlay appears

qterminal is XWayland but it's a terminal, so `vm-gui key`'s
xdotool-based plain-key injection reaches it (unlike modifier combos
into Qt apps — see AGENTS.md ).

```bash
# Click into the qterminal content area first — under the GUI test compositor, keyboard
# focus can lag xdotool's `search` unless the window has been explicitly
# clicked. A plain click on the terminal body does not send a keystroke
# to the TUI (it just transfers focus).
$VMGUI "$VM" click 640 400
sleep 0.3
# `?` = Shift+/ at evdev; vm-gui key no-ops on some XWayland paths.
virsh send-key "$VM" --codeset linux --holdtime 100 KEY_LEFTSHIFT KEY_SLASH
sleep 1
$VMGUI "$VM" screenshot /tmp/05-tui-help-overlay-s2-help-open.png
```

**Assert (help open):**
- A bordered modal panel covers most of the content area. The
 outer main-view header/footer may still be partially visible
 around it; that's expected (Textual ModalScreen).
- The modal's first line is bold `qdistro admin TUI`.
- The modal contains sections labelled **Decide current:**, **Scope:**,
and **Navigation:** in that order.
- Under "Scope:", all eight numbered lines are present:
 `1 Just this once`, `2 1 hour`, `3 24 hours`,
 `4 Forever, any command from this user`,
 `5 Forever, only this exact program`,
 `6 Forever, only this exact argv tuple`,
 `7 Forever, this argv basename anywhere`,
 `8 Forever, this argv prefix + any trailing args`.
- Under "Decide current:", both `a / Ctrl+Y` (Approve) and `d / Ctrl+N`
(Deny) are listed.
- The last paragraph mentions "mirrored to the GUI app instantly".

### S3 — Escape dismisses, main view returns intact

```bash
virsh send-key "$VM" --codeset linux KEY_ESC
sleep 1
$VMGUI "$VM" screenshot /tmp/05-tui-help-overlay-s3-dismissed.png
```

**Assert (after dismiss):**
- The modal is gone. Main view is visible again.
- Screenshot shows the same header/left pane/right pane as S1
 (no residual overlay artefacts, no stray border fragments).
- Subtitle still reads `scope: Just this once`.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin qterminal 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_tui 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/qterminal-tui.log'
```

## Notes for the runner

- The HelpScreen bindings also accept `q`, `space`, `enter`, and `?`
 again as dismiss keys. Only Escape is asserted here to keep the
 scenario narrow. A broader key-coverage scenario is a possible
 follow-up; don't expand this one in-place.
- If the `?` key produces nothing (modal never appears), do not
 retry with different key names. Report FAIL — a regression in
 the `question_mark` binding is exactly what we want to catch.
