# qdlocker GUI test runner — for graphic-aware subagents

Sibling of [`qdwin/tests/gui/AGENTS.md`](../../../qdwin/tests/gui/AGENTS.md).
Same orchestrator / runner shape; same VM; different system-under-test:
this harness drives [qdlocker](../..) (the new standalone Python+QML
locker bound to `qdwin_locker_v1`), **not** the legacy lock UI that
lives in `qdshell/Modules/LockScreen/*.qml`.

Read this file before touching scenarios — the load-bearing assertion
in this suite (`05-keystroke-isolation.md`) is a *security boundary*,
not a feature check. A test author who hard-codes the legacy locker
path will produce a green run that proves nothing.

## Roles

- **Orchestrator** — picks scenarios from `NN-*.md` in this directory,
  spawns one runner subagent per scenario, aggregates PASS/FAIL.
  Usually the parent agent driving the work for the user.
- **Runner** — graphic-aware subagent given one scenario file + a
  target VM. Executes setup → steps → asserts and returns the report
  format documented at the bottom of this file.

The orchestrator runs scenarios **serially** against a single VM —
every Setup block re-engages or releases the locker, and two runners
overlapping will deadlock each other on the lock state. If you need
wall-clock parallelism, clone the base VM per
`qdistro/tier4-vm/spawn-tier4.sh` and pin one runner per clone.

## Environment

- Host: openSUSE Tumbleweed with `libvirt` + `virsh` + `socat`.
- Target: a libvirt domain on `qemu:///session` running:
  - greetd-qdwin.service active on tty3 with autologin as `admin`.
  - **qdlocker.service** enabled in `systemd --user` for admin
    (overlay it onto the base image with
    `qdlocker/scripts/install-into-guest.sh` before spawning).
  - **qdistro-fprintd-fake.service** enabled (system unit) for
    scenario 02.
- VM name: `$VMNAME` if set, else
  `virsh -c qemu:///session list --name --state-running | head -1`.
- **Ctrl-socket introspection (finding 02):** production gates the
  `status` / `unlock-result` / `prompt-text` commands OFF — only `lock`
  is served. These scenarios assert on `status`/`prompt-text`, so
  `qdlocker_session_healthy` installs a root-owned marker
  (`/etc/qdistro/locker-ctrl-introspection`, via
  `qdlocker_enable_introspection`) and restarts the unit before any
  scenario runs. Nothing to do by hand;
  just be aware production locker sockets answer `error: command
  unavailable` to those three commands.
- The shipping Quickshell qdshell does not implement the removed qdshell.py
  launcher ctrl commands. Start throwaway test applications directly in the
  guest user session (scenario 01 uses a transient `systemd-run --user` unit).
- The **qdlocker** ctrl-socket at `/run/user/1000/qdlocker.sock` is
  the load-bearing introspection surface for this harness — every
  lock-state assertion goes through it.
- Standard test password `Pa_ssw0rd45` (matches the guest's PAM admin
  account).

## What works (and what doesn't) on qdlocker

| Surface | Driveable how | Notes |
|---|---|---|
| Ctrl+Alt+L → locker engages | `qdwin_chord ctrl alt -- l` then `qdlocker_wait_for_lock` | qdwin's global hotkey emits `lock_requested(3=manual)` on `qdwin_locker_v1`; qdlocker calls `set_locked(1)`. If `qdlocker_wait_for_lock` times out, the C-side `bind_qdwin_locker` plumbing isn't wired — see `qdwin/doc/locker.md` |
| Password input while locked | **NOT** keyboard typed into a TextInput — keys arrive via `overlay_key` event on `qdwin_locker_v1`. Type via `qdwin_chord` / `qdwin_qmp_key` and assert via `qdlocker_ctrl status` (`prompt-len=N`) | If the prompt length advances on `qdshell_ctrl` instead, the overlay_key router isn't checking `qdwin->locker_resource` first — security regression, see scenario 05 |
| Forcing a lock without keyboard | `qdlocker_ctrl lock` | injects a synthetic `lock_requested(reason=manual)` into the controller; equivalent to Ctrl+Alt+L for non-keyboard scenarios |
| Forcing an unlock for cleanup | restart the user unit: `systemctl --user restart qdlocker.service` from inside the VM | tears the lock surface down, recreates it in the unlocked state |
| Lock-state introspection | `qdlocker_ctrl status` → `locked=<bool> prompt-len=<n> pam-ready=<bool>` | always-on; the load-bearing assertion in every scenario |
| Last-auth-result | `qdlocker_ctrl unlock-result` → `last=success\|failed\|none` | survives until the next lock cycle |
| Idle-trigger | wall-clock wait for `QDLOCKER_IDLE_MS` (default 300000 ms = 5 min) — scenarios shorten via a `qdlocker.service.d` drop-in (`QDLOCKER_IDLE_MS=8000`) + restart. NOT 3000: setup+vm-exec latency can exceed a 3s threshold and pre-lock the baseline (the test races itself) | watches `ext-idle-notify-v1`; scenario 03 |
| Lid close / suspend | VMs don't model a real lid. qdlocker's own `LogindWatcher` (`qdlocker/logind.py`) subscribes directly over the system bus to logind's `Session.Lock` (reason=1, lid; needs `HandleLidSwitch=lock` in a `logind.conf.d` drop-in) and `Manager.PrepareForSleep` (reason=2, suspend). Drive via the guest `qdistro-fake-lid-close` helper (emits `PrepareForSleep`) and assert `locked=True` + a `reason=suspend` journal line | scenario 04. No qdwin C-side involvement — the helper exercises the suspend (reason=2) path; see 04 for a true `Session.Lock` (reason=1) variant |
| Fingerprint match | `busctl --system call ... qdistro.FprintFake EmitMatch` (requires `qdistro-fprintd-fake.service`) | scenario 02 |
| Visual assertion | `qdwin_screenshot <file.png>` — wraps `virsh screenshot` | qdlocker UI renders on the LOCK layer; screenshot captures it. Cursor visibility depends on renderer (see qdwin pitfall #6) — never assert on cursor presence |

### What's intentionally NOT in the table

- **Mouse interaction with the lock UI.** qdlocker's lock surface is
  on the LOCK layer with a pointer grab; pointer events are absorbed
  by the compositor. There is no mouse path. Scenarios that need to
  "click unlock" instead trigger via `qdlocker_ctrl` or a keyboard
  Enter through the overlay_key channel.
- **PAM password sent via ctrl-socket.** qdshell historically had an
  `unlock-password <pw>` ctrl command. qdlocker deliberately does
  **not** — that would defeat the point of the
  overlay_key-routes-to-locker security boundary (scenario 05). Type
  via the keyboard path, full stop.

## Helper script

Source `qdlocker-helpers.sh` at the top of every scenario:

```bash
source "$(dirname "$0")/qdlocker-helpers.sh"
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"
qdlocker_session_healthy || { echo "session not up"; exit 2; }
```

`qdlocker-helpers.sh` sources `qdwin-helpers.sh` from the qdwin
sibling repo (so `qdwin_send_key`, `qdwin_chord`, `qdwin_screenshot`, and
`qdwin_qmp_key` work as documented in qdwin's AGENTS.md) and adds
locker-specific accessors. qdlocker core scenarios use the qdlocker socket,
Quickshell IPC where explicitly required, and direct VM commands; they must
not depend on the removed qdshell.py ctrl API.

- `qdlocker_ctrl <command>` — talks to `/run/user/1000/qdlocker.sock`
  via the guest's socat. Commands: `status`, `lock`,
  `unlock-result`, `prompt-text`.
- `qdlocker_wait_for_lock [timeout=5]` — polls `qdlocker_ctrl status`
  until `locked=True`.
- `qdlocker_wait_for_unlock [timeout=5]` — polls `qdlocker_ctrl
  unlock-result` until `last=success`.
- `qdlocker_assert_prompt_len <N>` — fails non-zero if `prompt-len`
  isn't exactly N.
- `qdlocker_unlock_with_password [password=Pa_ssw0rd45]`
  and `qdlocker_drain_lock_state` — unlock through the real keyboard
  overlay path. Prefer this for cleanup; restarting qdlocker while
  qdwin is locked is fail-safe and may leave the compositor locked.
  If password unlock cannot drain a stale locked state,
  `qdlocker_drain_lock_state` restarts the qdwin user session as a
  test-cleanup fallback.
- `qdlocker_assert_color_present_in_crop <png> <#rrggbb> <WxH+X+Y> [label]`
  and `qdlocker_assert_color_absent_in_crop ...` — host-side
  ImageMagick screenshot checks for sentinel pixels. Use these for
  lock-screen occlusion; do not rely on agent vision alone for "no
  desktop pixels are visible."
- `qdlocker_session_healthy` — checks that `qdwin-compositor.service`
  and `qdlocker.service` are active and that qdlocker's ctrl-socket
  responds. It intentionally does not require qdshell's optional
  ctrl-socket; qdlocker is a peer process and several scenarios are
  specifically about surviving without qdshell.

## Hard-learned pitfalls (locker-specific — read before commands)

1. **qdlocker is a `systemd --user` unit, not `systemd` system.**
   `systemctl status qdlocker.service` as root inside the VM returns
   "not found." `vm-exec` runs as root, which has no user-session bus, so
   you MUST switch user AND supply the session runtime dir — bare
   `runuser -u admin -- systemctl --user ...` fails with
   "DBUS_SESSION_BUS_ADDRESS not defined" and any restart/dropin silently
   no-ops. The right invocation is
   `runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user status qdlocker.service`
   (or `runuser -l admin -c 'systemctl --user ...'`, which sets it via
   pam_systemd).

2. **Resetting the locker between scenarios.** If a scenario fails
   mid-cycle leaving `locked=True`, the next scenario's setup will
   inherit the locked state and try to type into the wrong context.
   Every Setup block must include the drain:

   ```bash
   qdlocker_drain_lock_state
   ```

   Do not use qdlocker service restart as the normal unlock path:
   qdwin intentionally treats locker loss while locked as fail-safe.

3. **The overlay-key path is silent on failure.** If qdwin's
   `bind_qdwin_locker` isn't wired and qdlocker isn't receiving
   `overlay_key` events, typed characters silently fall through.
   `qdlocker_ctrl status` will keep reporting `prompt-len=0` no
   matter how many keys you send. Always assert prompt-len after
   typing — DO NOT trust "screenshot shows password dots" alone, the
   dots might be coming from a stale TextInput.

4. **`qdwin_chord ctrl alt -- l` only fires the manual lock binding
   if the modifier-release transition is clean.** qdwin's hotkey
   channel checks "modifier alone" semantics (see qdwin AGENTS.md
   pitfall: weston `add_modifier_binding`). Don't substitute
   `qdwin_send_key KEY_LEFTCTRL KEY_LEFTALT KEY_L` — that path holds
   all three keys then releases simultaneously and the binding never
   fires.

   The promotion can also bisect that chord: its release events may arrive
   while the lock overlay owns input, leaving the restored normal seat with a
   stale modifier. After unlocking, call `qdwin_release_modifiers` before any
   assertion that types ordinary text into a client. This is an idempotent
   seat resynchronization, not a retry or a weakened text assertion.

5. **The fingerprint subscription is one-shot.** scenario 02 calls
   `EmitMatch` on the fake fprintd; if you call it twice in the same
   lock cycle, the second emission is dropped (the subscription was
   torn down on the first match). Re-lock between fingerprint
   attempts.

6. **qdshell's old lock module may still be loaded.** During the
   transition window, `qdshell/Modules/LockScreen/*.qml` is wired in
   parallel with qdlocker. If both are active, `qdwin_chord ctrl
   alt -- l` fires *both*, and the screenshot may show two lock
   surfaces stacked. Until qdshell drops its locker:

   ```bash
   "$QDWIN_VM_EXEC" "$VMNAME" \
     'runuser -u admin -- sh -c "echo lockScreen=false >>~/.config/qdshell/overrides.json"; \
      systemctl --user restart qdshell.service'
   ```

   Scenarios document this with a `# Pre-transition workaround:` note
   at the top of Setup so a future cleanup pass can remove it.

7. **qdlocker's ctrl-socket only listens after the QML root window
   has materialized.** A scenario that calls `qdlocker_ctrl` within
   <100ms of `systemctl --user restart qdlocker` will get
   "Connection refused." Use `qdlocker_wait_for_lock`/`_unlock`
   wrappers which retry, or sleep 2 explicitly after a restart.

8. **screenshot can capture mid-transition frames.** The LOCK-layer
   composite isn't atomic with `set_locked(1)` returning. Always
   `qdlocker_wait_for_lock` before screenshot-asserting "the lock UI
   is visible." Same on unlock: wait via `qdlocker_wait_for_unlock`,
   then screenshot.

## Available scenarios

| File | What it covers |
|---|---|
| [01-lock-cycle.md](01-lock-cycle.md) | Ctrl+Alt+L → type password → Enter unlocks → keyboard reaches focused toplevel again. End-to-end smoke; equivalent of qdwin's 03-locker-cycle but on the new path. |
| [02-fprintd-fallback.md](02-fprintd-fallback.md) | fprintd `VerifyStatus("verify-match")` unlocks with an empty prompt buffer. Confirms the parallel D-Bus subscription. |
| [03-idle-lock-trigger.md](03-idle-lock-trigger.md) | After `QDLOCKER_IDLE_MS` of no input, qdlocker engages the lock via the `ext-idle-notify-v1` subscription. |
| [04-lid-close-lock.md](04-lid-close-lock.md) | A systemd-logind signal reaches qdlocker's own `LogindWatcher` (no qdwin C-side path) and engages the lock. The fake helper emits `PrepareForSleep`, so it validates the suspend path (reason=2); the scenario also documents the true-lid `Session.Lock` (reason=1) variant. |
| [05-keystroke-isolation.md](05-keystroke-isolation.md) | **Security boundary.** While locked, password keystrokes reach qdlocker's `prompt-len` but NOT qdshell's. If qdshell's ctrl-socket sees the typed chars, the protocol's `overlay_key` routing is broken and the locker's purpose is defeated. |
| [06-shell-crash-survives.md](06-shell-crash-survives.md) | qdshell.service is killed while locked → the lock surface stays up → typing still reaches qdlocker → unlock still works. Confirms the lifecycle independence that motivated splitting qdlocker out of qdshell. |
| [07-lock-occludes-desktop.md](07-lock-occludes-desktop.md) | **Visual security invariant.** A full-screen magenta normal toplevel is placed behind qdlocker; after lock, screenshot edge bands and the full screen must contain zero magenta pixels. Catches fullscreen/first-map offset bugs. |
| [08-locker-crash-demotes.md](08-locker-crash-demotes.md) | **Resource cleanup.** qdlocker is killed while locked → qdwin demotes the lock toplevel (journal: `locker_disconnect`) → screen stays black (fail-safe) → fresh qdlocker binds and recovers → unlock works. |

A full smoke pass is 01 → 05 → 07 (skip 04 in guests without the
`qdistro-fake-lid-close` helper — the scenario as written needs only
that helper; the `HandleLidSwitch=lock` drop-in is required only for
04's optional true-lid `Session.Lock` variant). 06 and 08 are
regression-only — run after touching
qdshell/qdlocker or qdwin's resource-destruction paths.

## Running a scenario

1. Read the `NN-*.md` file top to bottom before acting.
2. Source `qdlocker-helpers.sh` and pin `VMNAME`.
3. Execute **Setup** verbatim. FAIL early if `qdlocker_session_healthy`
   returns non-zero.
4. Execute **Steps** in order. For each `qdwin_screenshot
   /tmp/<stem>-stepN.png`, immediately OPEN the PNG and look at it
   (`view_image` or equivalent) before moving on. OCR is not a
   substitute — it cannot see a colour, a layout, or that something
   is ABSENT. qdlocker's transitions are async — don't trust the
   prior step's "ok" until you have looked at the screenshot.
5. For each **Assert**, decide PASS/FAIL by combining:
   - `qdlocker_ctrl status` output (authoritative for state)
   - the screenshot (corroborates render)
   - `qdshell ctrl-socket` snapshot when comparing the two paths
     (scenarios 05, 06)

   Quote the relevant line of ctrl-socket output AND the relevant
   screenshot region. Don't FAIL on a screenshot alone if the
   ctrl-socket disagrees — the ctrl-socket is the source of truth
   for state per the project's "journal lines, not pixels" rule.
6. Execute **Cleanup** even on FAIL.

## Report format

Return one Markdown block:

```
# <scenario filename> — <PASS | FAIL | ERROR>

<when ERROR: one sentence on what broke in setup/teardown>

## Assertions
- [PASS|FAIL] <assertion id> — <one-line justification with ctrl-socket
  quote and/or screenshot region reference>
- ...

## Screenshots
- /tmp/<stem>-step1.png
- /tmp/<stem>-step2.png
...

## Ctrl-socket transcript
qdlocker_ctrl status (step 2) → <line>
qdlocker_ctrl status (step 3) → <line>
qdshell_ctrl locker (step 3) → <line>      # scenario 05 only
qdlocker_ctrl unlock-result (step 4) → <line>

## Notes
<free-form: any deviations, suspicions, or follow-ups>
```

For ERROR (setup/teardown crash), no Assertions block — just the one
sentence + the failing command output.

## Every scenario MUST declare `qci:visual`

Put exactly one of these HTML comments near the top of every scenario file:

```
<!-- qci:visual: required -->   a REQUIRED assertion is decided by reading a captured frame
<!-- qci:visual: none -->       no required assertion is decided by pixels
```

`none` is correct even when the scenario captures screenshots, as long as every
required assertion is settled by a non-visual oracle (journal line, D-Bus reply,
sqlite row, exit code, IPC response). Screenshots kept purely as run artifacts
do not make a scenario `required`.

`qci gui` REFUSES to run a scenario with no declaration, an unknown value, or
two conflicting declarations (`gui_validate_scenarios`) - before any golden
bake, VM, or agent. There is no content-sniffing fallback.

**OPEN EVERY FRAME YOU ASSERT ON.** Use your image-viewing tool (`view_image`
or equivalent). OCR is NOT a substitute: it reads text and nothing else, so it
cannot establish a colour, a geometry/layout claim, focus, z-order, or the
ABSENCE of a control - and "the pane is empty" / "no dialog appeared" / "the
badge is gone" are the commonest assertions here. Text OCR does not find is
indistinguishable from text it could not read, so OCR on an unrendered frame
produces a confident wrong verdict in either direction. Run OCR only to pull
long text out of a frame you have ALSO opened. If you cannot open images at
all, record ERROR naming the missing capability - never PASS, never FAIL, and
never fall back to OCR and grade anyway.

For a `required` scenario the gate also reads the frames itself, host-side,
after the agent exits: it checks every attested frame is DECODABLE and, when a
tesseract backend is present, records what text it finds. Both are recorded in
`visual-evidence/manifest.tsv`; the OCR column is corroboration and changes no
verdict. So the one thing a `required` scenario must do is SAVE THE FRAMES IT
ASSERTS ON into `$QCI_GUI_ARTIFACT_DIR`. A `required` PASS/FAIL whose artifact
directory holds NO attested frame, or whose frames are ALL undecodable, is
recorded ERROR - that is the harness failing to capture, not a verdict. Nothing
the agent writes (its own OCR output, its transcript) is accepted as evidence,
and artifact timestamps are never compared. Whether you opened a frame is
RECORDED per attempt as a diagnostic; it does not change your verdict, but it
is the first thing anyone reads when a visual verdict is disputed.
