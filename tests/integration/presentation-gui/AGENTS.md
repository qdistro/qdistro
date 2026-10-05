# Presentation GUI scenarios

Live checks that the shared appearance snapshot
(`/var/lib/qdistro/presentation/current.json`, published by qdshell) reaches
the first-party apps, the polkit prompt and the locker on a real qdwin
session. Pack 07 scenarios 1, 2 and the "other surfaces" from plan 01
(`todo/qdshell-apps/`). They run on the **qdwin** GUI lane
(`ci/lib/gates/gui.sh` routes `tests/integration/presentation-gui/NN-*.md`
there).

## Helpers

Source `presentation-helpers.sh` from this directory (it sources
`qdwin/tests/gui/qdwin-helpers.sh`):

| Helper | Use |
| --- | --- |
| `pres_admin '<cmd>'` | run a guest command as admin inside the session env; quoting-safe |
| `pres_qs_ipc <target> <fn> [args]` | `qs ipc call` into qdshell (`darkMode setDark`, `settings openTab display/0`) |
| `pres_snapshot` / `pres_wait_mode <mode> [s]` | read / wait for `generation mode` of the snapshot |
| `pres_launch <tag> <cmd>` | start an app detached in the session (`/tmp/pres-<tag>.log`) |
| `pres_app_pids` | `qfileman=… qterminator=… qdbrowser=… qnotebook=…` |
| `pres_focus_app <name>` | raise/focus the app's newest window (qdwin `focusWindow`) |
| `pres_output_scale` | integer wl_output scale of Virtual-1 |
| `pres_kill_apps` | stop everything these scenarios start |

Screenshots: only `qdwin_screenshot` frames count as evidence; OPEN every
frame before asserting on it (`ci/prompts/gui-scenario-agent.md`).

## Grading rules specific to this directory

- "Chrome" is the window's own UI: menu bar, toolbar, side panels, tab bar,
  dialog background and buttons. Web page content in qdbrowser and document
  content in qnotebook are CONTENT; they are not graded as chrome.
- DARK / LIGHT means the dominant chrome background is dark with light text,
  or light with dark text. Exact colours are not asserted from pixels; the
  snapshot generation/mode lines are the exact oracle.
- Display scale changes go through qdshell Settings > Display only: qdwin
  rejects output configuration from any other client (F8). The confirm
  dialog reverts after 15 s; move the pointer onto **Keep changes**, then
  click.
- Polkit: CI sessions run under a lingering user manager, so the polkit
  agent cannot register with polkitd; scenario 03 starts the prompt binary
  the way the agent does. Do not try to make `pkexec` work.
