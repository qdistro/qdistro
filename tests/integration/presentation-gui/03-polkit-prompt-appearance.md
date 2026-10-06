# 03 — the polkit prompt takes the trusted snapshot once, cancels cleanly

<!-- qci:visual: required -->
<!-- qci:visual-captures: 3 -->

**Acceptance criterion (plan 01 "Other surfaces", polkit):** the
`qdistro-polkit-prompt` dialog the polkit agent spawns reads the MANAGED
snapshot once at start (`role="polkit"`, `watch=False`) and ignores a
developer `QDISTRO_PRESENTATION_FILE` override. It therefore opens with the
shell's current appearance and keeps it while open even if the shell
switches. Cancel exits with status 1 and nothing on stdout (stdout carries
the password on submit).

Keyboard focus: a newly mapped prompt does not currently receive keyboard
focus on qdwin (recorded in the tracker as a product finding), so an
Escape key may go to whatever had focus. The steps cancel by CLICKING the
dialog's **Cancel** button; record in your notes whether the bar's
focused-window chip named the prompt.

**Scope note (recorded in `09-leftovers-2026-10-04.md`):** CI VMs run the
admin session under a lingering user manager, not a logind seat session, so
`qdistro-polkit-agent` cannot register (`Cannot determine session the caller
is in`) and a real `pkexec` never reaches it. This scenario therefore
starts the prompt binary exactly as the agent does (same path and
arguments) instead of going through polkitd. It covers the presentation
behaviour of the dialog, not the agent's registration.

## Setup

```bash
source "${QDISTRO_REPO}/tests/integration/presentation-gui/presentation-helpers.sh"
qdwin_set_vm "${VMNAME:?qci sets VMNAME to this scenario's VM}"
qdwin_session_healthy || { echo "FAIL: qdwin session not healthy"; exit 2; }
pres_kill_apps
qdwin_vmx_merged "test -x /usr/local/bin/qdistro-polkit-prompt && echo prompt-installed"

# pres_prompt_start <tag>: start the prompt detached exactly as the agent
# does, with a developer override that a polkit-role reader must IGNORE.
pres_prompt_start() {
    pres_admin "rm -f /tmp/pk-$1.*; setsid sh -c 'QDISTRO_PRESENTATION_FILE=/nonexistent/current.json /usr/local/bin/qdistro-polkit-prompt --mode=pam --action=org.qdistro.presentation.test --message=\"Presentation test prompt\" > /tmp/pk-$1.out 2>/tmp/pk-$1.err; echo \$? > /tmp/pk-$1.rc' >/dev/null 2>&1 </dev/null &"
}
```

**Assert (0.1):** the setup printed `prompt-installed`.

## Steps

### Step 1 — dark shell: the prompt opens dark

```bash
pres_qs_ipc darkMode setDark
pres_wait_mode dark 30
pres_prompt_start dark
sleep 4
qdwin_screenshot /tmp/pres03-step1-prompt-dark.png
```

**Assert (1.1):** OPEN the frame. An "Authentication Required" dialog with
the text "Presentation test prompt", an `Action:` line, a `Password:` field
and OK/Cancel buttons is visible. Its background is DARK and its text light
(the shell's dark snapshot, not a light/native fallback).

### Step 2 — the shell switches to light while the prompt is open: it stays

```bash
pres_qs_ipc darkMode setLight
pres_wait_mode light 30
sleep 3
qdwin_screenshot /tmp/pres03-step2-prompt-still-dark.png
```

**Assert (2.1):** OPEN the frame. The shell bar is now LIGHT, and the SAME
prompt dialog is still DARK (the trusted prompt reads once and does not
watch; it never restyles mid-authentication).

### Step 3 — Cancel exits with rc 1 and empty stdout

Click **Cancel** using coordinates read from the Step 2 frame you opened:
`qdwin_mouse_move X Y`, `sleep 0.5`, then `qdwin_click X Y`. Then:

```bash
sleep 2
pres_admin 'echo rc=$(cat /tmp/pk-dark.rc 2>/dev/null); echo stdout_bytes=$(wc -c < /tmp/pk-dark.out 2>/dev/null)'
```

**Assert (3.1):** `rc=1` and `stdout_bytes=0`.

### Step 4 — a new prompt opens light

```bash
pres_prompt_start light
sleep 4
qdwin_screenshot /tmp/pres03-step4-prompt-light.png
```

OPEN the frame, then click its **Cancel** button the same way (move, sleep
0.5, click), then:

```bash
sleep 2
pres_admin 'echo rc=$(cat /tmp/pk-light.rc 2>/dev/null); echo stdout_bytes=$(wc -c < /tmp/pk-light.out 2>/dev/null)'
```

**Assert (4.1):** OPEN the frame: the new prompt's background is LIGHT with
dark text, and its text and buttons are readable.
**Assert (4.2):** `rc=1` and `stdout_bytes=0`.

## Cleanup

```bash
pres_kill_apps
pres_qs_ipc darkMode setDark >/dev/null
```

## Pass criteria

All asserts 0.1 → 4.2 pass.

## Known-broken-if

- 1.1 light/native while the shell is dark: the prompt honoured
  `QDISTRO_PRESENTATION_FILE` (must be ignored for `role="polkit"`) or the
  managed snapshot is unreadable to admin.
- 2.1 dialog restyled: the polkit role is watching (`watch=False` required).
- 3.1 non-empty stdout on cancel: a password-path leak.
- 3.1 no rc file / prompt still running: the Cancel click missed; re-open
  the frame and click again once. A second miss is ERROR (driver).
