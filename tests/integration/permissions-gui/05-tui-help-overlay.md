# 05 — TUI `?` opens help overlay; any key dismisses

<!-- qci:visual: required -->

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
$VMGUI "$VM" screenshot-fresh /tmp/05-tui-help-overlay-s1-main.png
```

**Assert (main view):**
- TUI header reads `qdistro admin approvals (TUI)` with subtitle
 containing `scope: Just this once`.
- Left pane shows the `uid action exe` table header (content may be
 empty — scenario does not inject a request).
- Right pane shows `(no request selected)`.
- No modal / overlay is on top of the main view.

### S2 — press `?`, help overlay appears

qterminal needs the documented X focus handoff before evdev input. Mouse
clicks are platform-blocked on this template; activate the terminal window,
then inject `?` through the virtual keyboard (see AGENTS.md).

```bash
# Activate the named XWayland terminal before the evdev burst. This is the
# blessed focus path; do not replace it with a pixel click.
$VMEXEC "$VM" 'runuser -u admin -- env DISPLAY=:0 xdotool search --sync --name "Shell No. 1" windowactivate --sync'
# `?` = Shift+/ at evdev.
virsh send-key "$VM" --codeset linux --holdtime 100 KEY_LEFTSHIFT KEY_SLASH
# MANDATORY SETTLE WAIT -- do not drop or shorten it. There is no guest-side
# signal for "the modal is on screen", so time is the only wait-for-state
# available. A capture taken ~0.1 s after the key still shows the main view
# (reproduced 2026-09-25,
# todo/test-blankscreenshots/pg05-live-repro/b-s2-nosleep.png), and it is still
# accepted as "fresh" because the header clock changed.
sleep 2
$VMGUI "$VM" screenshot-fresh /tmp/05-tui-help-overlay-s2-help-open.png \
  /tmp/05-tui-help-overlay-s1-main.png
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

**Freshness is necessary, not sufficient.** `screenshot-fresh` refuses a
frame whose RAW pixels equal S1's (compare raw identities, `raw_pix_sha` in
field 4 of each `.raw` sidecar, never PNG file hashes: every frame is padded to
a size of its own). Identical raw pixels mean the capture path was stale, not
that `?` failed. But the header has a live clock, so a frame is "fresh" one
second later **whether or not the TUI has processed the key**: a different raw
hash proves only that a new frame was captured, never that `?` took effect.
Only the frame's content says whether the overlay is up.

If S2 shows the main view (no modal), the key may not have been rendered yet:
wait 2 more seconds and capture once more to a NEW name
(`$VMGUI "$VM" screenshot-fresh /tmp/05-tui-help-overlay-s2-help-open-late.png /tmp/05-tui-help-overlay-s1-main.png`),
open it, and judge S2 from that frame. Record FAIL only if the late frame also
shows no modal.

### S3 — Escape dismisses, main view returns intact

```bash
virsh send-key "$VM" --codeset linux KEY_ESC
# MANDATORY SETTLE WAIT, same reason as S2: a capture right after Escape still
# shows the overlay
# (todo/test-blankscreenshots/pg05-live-repro/b-s3-nosleep.png). Textual also delays a
# bare Escape briefly while it waits to see whether an escape sequence follows
# (Textual's escape delay).
sleep 2
$VMGUI "$VM" screenshot /tmp/05-tui-help-overlay-s3-dismissed.png
```

If S3 still shows the overlay, apply the same rule as S2: wait 2 more seconds,
capture once more to a NEW name (`.../05-tui-help-overlay-s3-dismissed-late.png`),
and judge from that frame.

**Assert (after dismiss):**
- The modal is gone. Main view is visible again.
- Screenshot shows the same header/left pane/right pane as S1
 (no residual overlay artefacts, no stray border fragments).
- Subtitle still reads `scope: Just this once`.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin qterminal 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_tui 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /home/admin/.local/state/qdistro/qterminal-tui.log'
```

## Notes for the runner

- The HelpScreen bindings also accept `q`, `space`, `enter`, and `?`
 again as dismiss keys. Only Escape is asserted here to keep the
 scenario narrow. A broader key-coverage scenario is a possible
 follow-up; don't expand this one in-place.
- If the `?` key produces nothing, first prove the TUI is alive and that the
 settle wait and the one late re-capture were both done. A fresh S2 (raw
 pixels differ from S1) is required but proves nothing about the key: the
 clock alone changes the frame. Do not retry with different key names. Once
 those preconditions hold, report FAIL — a regression in the `question_mark`
 binding is exactly what we want to catch.
