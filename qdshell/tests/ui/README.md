# qdshell agent-assisted UI tests

A regression harness that drives each panel / settings tab via `qs ipc`,
screenshots the result, and uses a vision LLM to verify the captured
image still matches a developer-authored description.

The goal is to catch *behaviour regressions* during refactors (e.g.
when extracting a `PanelShell` base or collapsing settings tabs)
without requiring pixel-perfect goldens.

## Two transports

The harness supports two ways to reach a running qdshell:

* **VM transport (preferred — the `qci gui` gate path).** When
  `QDSHELL_UI_VM=<libvirt-domain>` is set, the harness drives the LIVE
  qdshell session already running inside a qdwin VM: IPC over wayland-1
  via `scripts/vm/vm-exec` (run as `admin`, targeting the deployed
  `qs -p /usr/share/quickshell/qdshell`), and screenshots qdwin's
  Virtual-1 output via the in-compositor shell-authorized capture
  (qdshell's root-only `capture` ctrl verb — `virsh screenshot` only sees
  the tty console on the headless VMs). codex describe/judge
  still run on the host against the pulled-back PNGs. This is the
  validated path: qdshell renders fine in a real qdwin session.

* **Host transport (legacy fallback).** When `QDSHELL_UI_VM` is unset, the
  harness boots a nested headless compositor + qdshell on the host. This
  path is known to SIGSEGV quickshell during early FileView settings load
  under headless Wayland on the current host
  (see `todo/qdwin-vm/agent-ui-harness-headless-quickshell-crash.md`); it
  is kept only for hosts with a working nested compositor and fails
  loudly rather than passing on a blank framebuffer.

The `qci gui` gate sets the VM env vars automatically
(`QDSHELL_UI_VM`, `QDSHELL_UI_VM_EXEC`, `QDSHELL_UI_VIRSH`) after it
acquires the GUI VM, and only runs the vision harness when a live
qdshell/noctalia session is detected on wayland-1.

### Security

Every IPC argument that reaches the VM's `/bin/sh -c` (via
qemu-guest-agent) is a developer-authored constant from `manifests.py`,
re-validated against a strict token allowlist (`runner._IPC_TOKEN_RE`)
before being shipped, and the guest command body is base64-wrapped (the
`scripts/vm/vm-script` idiom) so nothing dynamic is interpolated into the
guest shell string. The libvirt domain name from `QDSHELL_UI_VM` is also
allowlist-validated.

## How "what an agent would see" works

For every surface (Settings tab / slide-out panel / bar idle) there is
one human-authored markdown file under `expectations/` listing what
*must* be visible there.  At test time:

1. The harness opens the surface via `qs ipc call …`.
2. `weston-screenshooter` saves a PNG of the framebuffer.
3. The PNG is sent to the local Codex CLI with a "describe what you see"
   prompt -> free-form bullet list of observed elements.
4. The observed description is judged against the expectation file by
   a second Codex call: every reference bullet must be present in the
   observed description for the test to pass.

Step 4 is LLM-as-judge rather than substring matching because UI text
descriptions naturally vary in wording.

## Requirements

System packages:

* **A wlroots-based nested compositor** (any one of):
  `sway`, `labwc`, `cage`, `wayfire`, `river`.
  qdshell uses `wlr-layer-shell` for every panel/bar surface;
  weston does *not* implement this protocol, so weston cannot be used
  to validate panel rendering. The runner auto-probes the candidates
  above in order and uses the first one it finds.
* `grim` — screenshot tool for wlroots (`zypper in grim` / `apt install grim`).
* `qs` / `quickshell`
* `python3` (>=3.10), `pytest`, and the local `codex` CLI

On the current dev machine (openSUSE Tumbleweed) install with:

```bash
sudo zypper install sway grim
```

Vision backend:

* `codex` — required for the describe + judge steps. Without it the harness
  still boots qdshell and captures PNGs into `tests/ui/artifacts/` so a
  human can compare manually, but every test reports SKIP. Set
  `QDSHELL_UI_NO_CODEX=1` to force the secondary local `pi` fallback when it
  is installed.

## Running

```bash
# Make sure pytest is installed and codex is on PATH.
pip install --user pytest

# Run the suite (set the env flag — the suite is opt-in so it does
# not fire in the default qmltest CI workflow that uses
# QT_QPA_PLATFORM=offscreen).
QDSHELL_UI_TESTS=1 pytest tests/ui -v
```

Run a single surface:

```bash
QDSHELL_UI_TESTS=1 pytest tests/ui -v -k settings_audio
```

Artifacts (PNG screenshots, weston/qdshell logs) land in
`tests/ui/artifacts/`. The directory is recreated on every run; the
PNG of `settings_audio` ends up at `artifacts/settings_audio.png`.

## Coverage

* **30 settings tabs** — all reachable via `settings openTab <name>`.
* **14 slide-out panels** — those with first-class IPC are driven; any
  that don't expose a toggle handler in current qdshell appear in the
  manifest with `NO_IPC` and the test uses `pytest.xfail` to flag them
  rather than silently skip.
* **Bar** — one idle screenshot is captured to baseline overall
  bar/dock/widget layout.

### Stateful-interaction + real-input depth (VM-only)

Beyond "the surface opens", these assert concrete STATE and survive a real
qdshell restart. They require the VM transport (persisted config, the user
systemd unit, the qdshell ctrl-socket, and QEMU QMP input injection have no
host nested-compositor equivalent); without `QDSHELL_UI_VM` they SKIP with a
precise reason. The host-runnable depth for the pure logic they exercise lives
in the Node suites `tests/test_launcher_navigation.js` and
`tests/test_settings_recovery.js`.

* `test_interaction.py` — a toggle flipped via IPC lands in `settings.json`
  and survives a `systemctl --user restart qdshell.service` (read back from
  disk); color-scheme selection round-trips; a sequence of panel open/close
  cycles leaves a clean idle bar (state across multiple opens, not isolated
  captures).
* `test_degraded.py` — each panel (Bluetooth/Network/Audio/Battery/Media/Tray)
  renders a coherent empty/unavailable state under a missing backing service
  (judged against `expectations/panel_*_degraded.md`), never a blank panel or
  a crash.
* `test_config_recovery.py` — a truncated / empty `settings.json` does not
  brick the shell: qdshell recovers to defaults, restarts, answers IPC, and
  rewrites a valid config.
* `test_real_input.py` — REAL keyboard/mouse via QMP `input-send-event` (the
  same evdev-layer path a physical keyboard takes, NOT the ctrl-socket
  shortcut): typing into the launcher search field, arrow/Enter/Esc nav,
  mouse-click result selection, Shift/Backspace handling, and a real
  password+Enter locker unlock. Some of these exercise the documented qdwin
  overlay-keyboard-grab gap (see `qdwin/tests/gui/qdwin-helpers.sh` header) and
  are written to fail loudly so they pin the fix when qdwin lands the grab. The
  locker test needs `QDSHELL_UI_VM_PASSWORD` and skips without it (no
  credential is hard-coded).

## Updating expectations after a refactor

If you intentionally change a surface (e.g. you merge OSD into User
Interface), edit the relevant `expectations/<surface>.md` to reflect
the new contract, commit it, then re-run the suite.

## Why this harness, not pixel diffs

Pixel diffs would tie the test to a specific font / DPI / wallpaper.
Vision + judge tolerates incidental visual drift (a slightly different
gradient, a re-ordered card) while still catching *meaningful*
regressions (a section heading disappeared, a slider lost its label).

## Why headless weston, not the host compositor

* The host here is KWin/Plasma; qdshell is built for Hyprland/Niri and
  would overlap plasmashell unpleasantly.
* CI containers have no GPU; weston headless + pixman software
  rendering works anywhere.
* Tests get a deterministic 1920×1200 framebuffer regardless of host.

## Files

* `runner.py` — primitives: VM-session transport (IPC + shell-capture)
  and the legacy host Weston/Qdshell lifecycle, plus IPC, screenshot,
  describe, judge. Also the stateful-interaction transport: settings.json
  read/write, qdshell restart, ctrl-socket, and real keyboard/mouse via QMP
  (`tap_key`/`chord`/`type_text`/`mouse_click`).
* `manifests.py` — the canonical surface list.
* `conftest.py` — pytest fixtures: a unified `capture` fixture that routes
  to the VM transport when `QDSHELL_UI_VM` is set, else the host transport;
  and a `vm_session` fixture that SKIPs when no VM is provided (used by the
  stateful-interaction / real-input suites).
* `test_settings_tabs.py`, `test_panels.py`, `test_bar.py` — surface-render
  regression cases.
* `test_interaction.py`, `test_degraded.py`, `test_config_recovery.py`,
  `test_real_input.py` — VM-only stateful-interaction + real-input depth.
* `expectations/` — one `.md` per surface (incl. `panel_*_degraded.md`).
* `artifacts/` — PNGs + logs from each run (git-ignored).
