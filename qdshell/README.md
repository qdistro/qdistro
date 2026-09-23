# qdshell

A Wayland desktop shell for [qdistro](https://github.com/qdistro/qdistro)
— bar, panels, launcher, notifications, OSD, and settings — running on
top of the [qdwin](https://github.com/qdistro/qdwin) compositor.

## Role in qdistro

qdshell is the trusted desktop shell for the admin session. It renders the bar,
panels, launcher, notification surfaces, OSD, settings, and qdistro-specific
controls that let the owner see and operate silos. It is not the compositor and
not the security broker; those live in [qdwin](../qdwin) and
[qdistro](../qdistro) respectively.

The runtime lock surface has moved out to [qdlocker](../qdlocker), so qdshell
can crash or restart without owning the unlock decision.

qdshell is a hard fork of [Noctalia](https://github.com/noctalia-dev/noctalia-shell)
v4.5.0. The upstream history is preserved in this repository (reachable
from the `fork-base/upstream-v4.5.0` tag); fork-local changes were
collapsed into a small set of thematic commits when qdshell was
published. See [CREDITS.md](CREDITS.md).

## What's different from Noctalia

- Single compositor: qdshell only targets qdwin. The
  `CompositorService` abstraction was dropped — qdshell binds Qdwin
  APIs directly. A guard test forbids foreign-WM dispatch code from
  re-entering the tree.
- Broker integration: every hook script and notification is mediated
  by the qdistro broker. Runtime locking is delegated to qdlocker over
  its control socket.
- Strip pass: telemetry, the update channel, supporter banner, setup
  wizard, changelog, about box, wallhaven, and GitHub release plumbing
  were removed. The upstream migration chain was reset to schema v1.

## Repository layout (sibling checkout)

For development, qdshell expects the qdistro umbrella checked out as
a sibling (`../qdistro/`) — the test scripts in `scripts/` look there
for bats tests + the broker source. Canonical layout:

```
qdistro-org/
├── qdistro/     ← umbrella (broker, tests, scripts)
├── qdwin/       ← compositor
└── qdshell/     ← this repo
```

See the [qdistro umbrella README](https://github.com/qdistro/qdistro)
for the full clone sequence.

## Build & run

The QML tree itself has no build step. Two pieces do get built/installed:

- **Native QML plugin** (`qml-plugin/`): a small Qt plugin, built with
  meson, that binds the `qdwin_shell_v1` IPC into QML. This is the only
  thing meson builds here — meson does not install the QML tree.
- **Deployment**: the real session install (QML tree to
  `/usr/share/quickshell/qdshell` plus the plugin) is done by the
  umbrella repo's `scripts/install/install-qdwin-session-for-vm.sh`,
  which is what the bootstrap and the CI VM provisioning use.

For a quick host-side preview outside a qdwin session:

```sh
quickshell -p shell.qml
```

Note that many surfaces need a live qdwin compositor (and broker) to do
anything meaningful — the supported way to see qdshell working is inside a
qdistro test VM (see below).

## Testing

Coverage lives in three layers, driven by two scripts:

```sh
scripts/ci-local.sh     # host gates: qmltestrunner (Tests/tst_*.qml),
                        # Node JS unit tests (tests/test_*.js), qmllint
                        # (informational), qmlformat check; optional bats
                        # integration. Flags: --strict --no-int --quick
QDISTRO_VM=<vm> scripts/ci-in-vm.sh   # runs the qmltest suite inside a
                        # qdistro VM; --bats adds broker end-to-end bats
```

- `tests/test_*.js` — plain Node unit tests (also wired as `meson test`).
  This is where the security-critical gate logic is covered: clipboard
  brokering, fail-closed broker gates, silo-identity drift guards, input
  config, taskbar behaviour.
- `Tests/tst_*.qml` — qmltestrunner smoke tests.
- `tests/ui/test_*.py` — agent-assisted live-VM UI tests (pytest); they
  only execute with `QDSHELL_UI_TESTS=1` and `QDSHELL_UI_VM=<vm>` set.
- `tests/test_*.py` — host-side Python unit tests (`python3 -m pytest
  tests`). Not currently wired into `ci-local.sh`; run them manually when
  touching the areas they cover (bluetooth pairing, content signing,
  plugin helper, safe write, theming hooks).

Host `qmllint` cannot resolve Quickshell's `qs.*` modules, so
`.qmllint.ini` relaxes the categories that would false-positive; full QML
validation happens inside a VM where the modules resolve.

If you edit tests, read `tests/AGENTS.md` first — it codifies a strict
never-reduce-coverage policy.

## License

GPL-3.0-or-later — see [LICENSE](LICENSE).

Upstream Noctalia is MIT-licensed; the MIT license permits relicensing
to GPL. Attribution to upstream contributors is in
[CREDITS.md](CREDITS.md).
