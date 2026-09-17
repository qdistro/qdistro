# qdwin-apps GUI test scenarios — for orchestrator + runner subagents

These scenarios verify **third-party application compatibility** with
the qdwin compositor. Distinct from `phase1/gui-tests/qdwin/`, which
exercises qdshell (panel/launcher/locker). Here qdshell is **not**
running — instead a minimal `qdwin-bystander` C client (built from
`test-client/qdwin-bystander.c`) plays the role of a v14
shell: it releases the held-layer for each new toplevel and accepts
`max` / `restore` / `min` / `close` / `focus` commands on a FIFO at
`/run/user/1000/qdwin-cmd.fifo`.

These scenarios were authored 2026-05-05 alongside the four-bug-fix
landing in `qdwin/qdwin.c`. Each scenario double-checks one
qdwin protocol path against a real client.

## Bug coverage map

| Scenario | qdwin behaviour exercised |
|---|---|
| `01-firefox-max-restore.md` | `request_maximize(0)` returns to pre-max geometry (was bug #1) |
| `02-xterm-xwayland-launch.md` | XWayland surface attach doesn't NULL-deref (was bug #2) |
| `03-foot-vs-xterm-tagging.md` | `is_xwayland=1` for X11, `0` for Wayland in `toplevel_added` (was bug #3) |
| `04-cursor-spam-suppressed.md` | `install_default_cursor: no surface yet` logs at most once per session (was bug #4) |
| `05-gtk4-gnome-text-editor.md` | Native Wayland GTK4 toolkit |
| `06-gtk3-thunar-xwayland.md` | XWayland GTK3 toolkit + dbus-activated apps |
| `07-qt5-vlc.md` | Qt5 widget app via XWayland |
| `08-electron-chromium.md` | Electron via XWayland (not native ozone) |
| `09-wxwidgets-audacity.md` | wxWidgets via XWayland |
| `10-tk-fltk-swing.md` | Three "small toolkits" (Tk, FLTK, Java Swing) round-trip |
| `11-imlib2-feh.md` | Raw Xlib + Imlib2 (no toolkit at all) |
| `12-keystroke-roundtrip.md` | wl_keyboard delivery to focused native + XWayland clients |
| `13-rdp-subscribe-frame.md` | single-window RDP sharing: subscribe → qdistro-forward spawns → xfreerdp completes handshake + decodes frames |

## Running

The scenarios use a shared helper at `qdwin-apps-helpers.sh`. Source it
once per session:

```bash
source ${QDWIN_REPO}/tests/apps/qdwin-apps-helpers.sh
qdwin_apps_set_vm "${VMNAME:-apps-qdwin-...}"
qdwin_apps_session_up || { echo "FAIL: bystander/weston not healthy"; exit 1; }
```

`qdwin_apps_session_up` is **self-healing**: if the bystander/FIFO aren't
ready (fresh boot still running qdshell, or a bystander whose FIFO defaulted
elsewhere) it calls `qdwin_apps_become_shell` once and re-checks, so you do not
have to hand-roll the "kill qdshell, start bystander" dance — and it always
starts the bystander with `QDWIN_BYSTANDER_FIFO=/run/user/1000/qdwin-cmd.fifo`
so the FIFO lands where every helper polls. Pair it with
`qdwin_apps_restore_shell` in your Cleanup/Teardown to bring the desktop
`qdshell` back.

The helper provides:

- `qdwin_apps_become_shell` — deterministically take the shell role for the
  bystander: cleanly `systemctl --user stop qdshell.service` (a manual stop
  suppresses `Restart=`, so qdshell stays down without masking), evict stray
  `qs`, then launch the bystander with the canonical FIFO + wayland env and
  wait for the FIFO. Called automatically by `qdwin_apps_session_up` when
  needed; call it directly only if you want the takeover without the check.
- `qdwin_apps_restore_shell` — undo the above: stop the bystander, restart
  `qdshell.service`. Best-effort; safe to call in Cleanup.
- `qdwin_apps_prepare_shell_probe` — reserve the singleton shell role for a
  scenario-specific bystander: run the normal takeover, then stop its suite
  bystander, wait for qdwin's compositor journal to report `shell unbound`,
  remove the canonical FIFO, and fail if qdshell/bystander still owns the role.
  It restores qdshell on every failure. Launch and verify exactly one
  replacement immediately.
- `qdwin_apps_launch <name> <cmd>` — start an app as `admin` against the
  active wayland socket with the standard env (`MOZ_ENABLE_WAYLAND=1`,
  `QT_QPA_PLATFORM=wayland`, `GDK_BACKEND=wayland`, `DISPLAY=:0`).
- `qdwin_apps_ctl <command>` — push a line to the bystander FIFO. Prefer
  `qdwin_apps_ctl maxlast` / `restorelast` after launching one app, or pass an
  explicit handle such as `qdwin_apps_ctl "max 7"`.
- `qdwin_apps_screenshot <path>` — `virsh screenshot` to host path.
- `qdwin_apps_send_key KEY_*` / `qdwin_apps_type "string"` for discrete
  keys, and `qdwin_apps_chord alt -- f` for real modifier-held chords. These
  use QMP `input-send-event`; do not substitute `virsh send-key` for a chord.
- `qdwin_apps_kill <name>...` — stop only the named, allowlisted test apps
  using an argv[0]-anchored match. Prefer this in Cleanup when the scenario
  knows what it launched.
- `qdwin_apps_kill_all` — stop all allowlisted test apps, but never shared
  session infrastructure (`weston`, `Xwayland`, generic `python3`/`java`
  services). Use only as the Setup/Cleanup safety net.
- `qdwin_apps_log_grep <pattern>` — grep the current boot's
  `qdwin-compositor.service` user journal plus `/tmp/bystander.log`.
- `qdwin_apps_journal_cursor` / `qdwin_apps_log_since_cursor <cursor> <pattern>`
  — capture a pre-action qdwin journal boundary and inspect only its delta.

## As the runner subagent

Each `NN-*.md` is a self-contained scenario. Read it top to bottom,
follow Setup / Steps verbatim, return a PASS/FAIL report referencing
the screenshots. Same pattern as `phase1/gui-tests/qdwin/AGENTS.md`.

**Pitfalls** (read before running):

1. The active wayland socket is whichever weston picked (`wayland-1`
   or `wayland-2`); after a weston crash the names rotate. The helper
   re-detects on every call. Don't hard-code `wayland-1`.
2. `vm-exec` quoting is fragile — wrap multi-line shell in
   `base64 -w0 <<EOF / EOF` and decode in-VM. The helper does this
   automatically; if you write inline `vm-exec "$VM" 'one-liner'`,
   prefer single quotes and avoid embedded `"`.
3. The bystander prints its log to `/tmp/bystander.log`. Tail it before
   asserting — `toplevel_added handle=N` confirms qdwin saw the
   surface even when the screenshot is black.
4. **Cleanup matters.** Each scenario kills its own app in Cleanup;
   stale toplevels obscure the screen for the next scenario. The
   helper's `qdwin_apps_kill_all` is the safety net.
5. Native Wayland Qt apps (kate, qpdfview, qbittorrent) currently
   render black under the pixman renderer. Don't fail the unrelated
   scenarios on that — track via
    instead.

## As the orchestrator

Spawn one Sonnet subagent per scenario, **serially** on a given VM.
The bystander and stale-toplevel pollution mean concurrent scenarios
on one VM produce spurious FAILs. Spin up a second clone via
`qdistro/scripts/vm/clone-baseweed.sh apps-qdwin --from-baked` if
you need wall-clock parallelism.

Pre-conditions for app-deps runs:

- VM is running, weston is up, bystander is up, bystander FIFO is at
  `/run/user/1000/qdwin-cmd.fifo`.
- `firefox`, `thunderbird`, `xterm`, `xeyes`, `foot`, `gnome-text-editor`,
  `thunar`, `vlc`, `audacity`, `chromium`, `gpick`, `feh`, `eog`,
  `gedit`, `evince`, `inkscape` all installed (one-shot
  `zypper -n install` from the matrix run on 2026-05-05; bake into a
  fresh image when the matrix is stable).
- For the Tk/FLTK/Swing scenarios: `python313-tk`, `libfltk1_3`,
  `fltk-devel`, `java-25-openjdk-devel`, and an X11 bitmap font package
  (`xorg-x11-fonts` / `xorg-x11-fonts-core`, needed or Tk fails with
  `failed to allocate font`) installed; demo source files live under
  `tests/apps/demos/` (next to the scenarios) and are staged into the
  VM by scenario 10 itself via a private free-port host HTTP server
  rooted at that `demos/` directory (no fixed `:8765` port — that was a
  parallel-CI collision hazard). If Tk fails only on the font, report
  `INFRA: Tk font allocation failed`, not a compositor FAIL.

These heavy app dependencies are opt-in for lean GUI goldens. If an
app-specific package such as `chromium` is absent because the VM was
not built with `QDWIN_APP_DEPS=1`, report a clean `SKIP: <app> not
installed; qdwin app deps are opt-in`, not a compositor failure. If
the app-deps lane was explicitly requested and a listed package is
still missing, report `INFRA: <thing>` so the orchestrator can rebake
or fix the package list.

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
