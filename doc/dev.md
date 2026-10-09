# Developer guide

How to set up a machine, build, test, and iterate on qdistro. The
project-wide invariants (language policy, single-tenant assumptions,
commit conventions) live in [AGENTS.md](AGENTS.md); this file is the
practical side — toolchain, gates, images, and per-app conventions.

## Dev setup

qdistro is one repository: qdistro's own content at the root and the
components (`qdwin/`, `qdshell/`, `qdlocker/`, `qdgreeter/`, `qdbrowser/`,
`qdterm/`, `qdfileman/`, `qnotebook/`, the two browser extensions) as
top-level directories. One clone is the whole layout; no env vars, no
sibling checkouts, no system install of the sources:

```sh
git clone https://github.com/qdistro/qdistro.git
cd qdistro
```

For parallel work, use one git worktree per task
(`git worktree add .worktrees/<topic> -b <branch>`); a worktree is a
complete tree and needs nothing linked next to it. The checkout on `main`
is merge-only — work happens in worktrees and is merged in. See
[../AGENTS.md](../AGENTS.md) for the working workflow.

## Host prerequisites

The host only needs **orchestration** tools. All compilers, Qt, Python
test modules and headers live in a pinned container image, not on the
workstation:

- **Rootless Podman** — builds and headless tests run inside it.
- **Bash, Git, Python 3 (stdlib), Bats** — the qci runner itself.
- **libvirt + qemu-kvm + bubblewrap** — only for the VM gates (bats, gui,
  image). Nested KVM must be enabled for the tiers that run VMs inside
  the VM; see the warning in [../README.md](../README.md#try-qdistro).

Check what the host is missing:

```sh
ci/bin/qci-host-deps --check
ci/bin/qci preflight          # also verifies the libvirt session and bases
```

For the libvirt session (set up once):

```sh
sudo zypper install libvirt qemu-kvm virt-install virt-manager bubblewrap
sudo usermod -aG libvirt $(whoami)   # log out and back in
virsh -c qemu:///session list --all  # verifies the per-user session
```

If the session cannot connect, start the distro's system libvirt
service/socket (or open virt-manager) and repeat; `libvirtd.service` is
not a user unit.

The VM/image paths additionally need tools `qci-host-deps` does not
check: **libguestfs + guestfs-tools** (`virt-customize`, `virt-resize`,
`virt-cat`, `virt-sparsify`) for the base builders, `sshpass` and `jq`
for `image/verify.sh`, and ImageMagick's `magick` for the GUI gate's
frame-usability analysis.

## Building: the container toolchain

`ci/bin/qci host` runs every build and headless test row — for the root
and all ten components — inside one rootless Podman image pinned to the
Tumbleweed snapshot in [`snapshot.conf`](../snapshot.conf). The gate
builds qdwin's vendored, patched libweston from current source before
qdwin and qdshell, builds the C daemons and QML plugin, runs every pytest
and npm suite, and checks the QML stack.

```sh
ci/bin/qci host
# A single suite in the same toolchain, for development feedback:
ci/bin/qci-host-run bash -c 'cd qdlocker && python3 -m pytest -q tests/unit'
ci/bin/qci-host-run bash -c 'cd qdfileman && python3 -m pytest -q'
```

How the container behaves (see `ci/containers/` and
[../ci/README.md](../ci/README.md#host-test-dependencies)):

- The worktree is mounted at its original absolute path with
  `--userns=keep-id`; generated files keep your uid. No host home,
  display socket, D-Bus socket, or site-packages leaks in. Qt runs
  offscreen; a private `dbus-run-session` supplies both bus addresses.
- Build/test rows run with `--network=none`. The two browser
  extensions' `npm ci` runs in a separate *networked* preparation
  container; downloads cache under `$QDWIN_CACHE_DIR/host-npm/`
  (default `~/.cache/qdistro/`).
- The image is cached by dependency recipe, snapshot pin, and resolved
  base image ID — ordinary application-source changes reuse it (the
  recipe also covers the vendored QTermWidget binding sources, so changes
  there re-key the image).
- `QCI_OFFLINE=1` refuses to pull anything: it needs the image and the
  npm/dependency caches already warm.

For manual builds of the root daemons, run meson through
`ci/bin/qci-host-run` and point `PKG_CONFIG_PATH` at
`qdwin/build-qci/meson-uninstalled` after a host gate run. There is no
supported "native host" build path — don't install the toolchain on the
workstation.

## The Tumbleweed snapshot pin

[`snapshot.conf`](../snapshot.conf) at the repo root is the **one**
Tumbleweed pin. Everything reads it: the kiwi disk image's repositories
(`image/build.sh`), the cloud-derived test VM bases
(`scripts/vm/lib/test-substrate.sh`), the tier-2 workload images, and the
Podman builder base (`tumbleweed:<snapshot>`). One snapshot means one
download set and one RPM cache.

- The pin is an 8-digit `history/<snapshot>` id plus the SHA256 of the
  dated Minimal-VM cloud qcow2 (verified against openSUSE's signed
  checksum; its `VERSION_ID` must equal the snapshot).
- **The pin expires 14 days after its snapshot date.** The history
  service keeps ~4 weeks, so rotate deliberately — roughly weekly. Verify
  the new cloud image signature and `VERSION_ID`, update `cloud_url`,
  `cloud_sha256` and `snapshot` together, then rebuild the bases and the
  tester image. `QDISTRO_TEST_SUBSTRATE=<manifest>` runs experiments
  against a separate manifest.
- Cached RPMs under `$QDWIN_CACHE_DIR/{rpm,podman-rpm}/<snapshot>/<arch>/`
  are download hints only — repositories keep `gpgcheck=1`.

## Test VM bases

The default qci VM base is **not** the product image — it is the
openSUSE Minimal-VM cloud qcow2, dependency-baked:

| Base | Built by | Contains |
| --- | --- | --- |
| `baseweed-admin-<…>.qcow2` | `scripts/vm/build-baseweed-from-scratch.sh` (~5–10 min, once per pin) | cloud image + `admin` uid 1000, test password, qemu-guest-agent, first-boot wizards masked |
| `baseweed-baked-<…>.qcow2` | `scripts/vm/build-baked-baseweed.sh` (re-runnable; at-rest `virt-customize`) | runtime + test packages baked in; **no compilers** — native bits come from the Podman builder |
| `baseweed-enforcing-baked.qcow2` | `scripts/vm/build-enforcing-baseweed.sh` | `SELINUX=enforcing` + a host SSH key for the bats lane (qga is denied under enforcing) |

The admin/baked filenames carry the cloud SHA and snapshot, so a pin bump
produces new disks and never mutates bases that preserved VMs still boot
from. The enforcing bake is the exception: it is the fixed name
`baseweed-enforcing-baked.qcow2` and `--force` replaces it in place —
rebuild it after a rotation, but not while a run is using it.

Per run, `bats` and `gui` provision a **golden** qcow2 once — native
qdwin/qdshell/daemons/qsu binaries and SELinux modules built in Podman
from current source, layered over the base — and every worker clones it
(~10 s provisioning per VM). `QCI_NO_GOLDEN=1` disables this.

`QDISTRO_VM_BASE=kiwi` clones workers from a kiwi tester/`ci`-profile
image imported with `scripts/vm/import-kiwi-base.sh` — the path to use
when image parity is the test target (`auto` prefers an imported kiwi
base but silently falls back to baked). Manual driver
tools (`vm-exec`, `vm-gui`, `vm-start-and-wait`, `spin-test-vm.sh`) are
documented in [vm-dev-tools.md](vm-dev-tools.md).

## Testing: the gates

`ci/bin/qci` (or `just` in `ci/`) is the monorepo's CI. There is no
hosted CI. The gate list with full semantics is in
[../ci/README.md](../ci/README.md); the short tour:

| Gate | What it runs |
| --- | --- |
| `preflight` | Host tools, libvirt session, bases, in-tree components. |
| `lint` | shellcheck (warn), blocking bats syntax, doc link checks. |
| `selftest` | The qci runner's own contract suite (no VM). Runs first in `host`. |
| `host` | Every build + headless test row, in the container. |
| `vm-smoke` | One VM: session, Wayland socket, core user services. |
| `bats` | Every `tests/integration/vm/*.bats` (plus per-component ones), one disposable VM per file, in parallel. `# qci:host-only` files run on the host. |
| `gui` | Markdown GUI scenarios driven by a visual agent, plus the executable qdwin smokes. |
| `image` | Qualify `image/`'s built artifact: extract → static checklist → boot-verify. |
| `mmnet`, `snapshot-daily`, `release-manifest`, `bootstrap-release-profile`, `registry-check` | Multi-VM network lane; daily VM; release-contract checks. |
| `full` | preflight + host + release checks + image + vm-smoke + bats + gui (hours). |

Every run is self-contained under `ci/runs/<gate>-<utc>-<pid>/`:
`report.md`/`report.html`, `results.tsv`, `manifest.txt`,
`repo-state.tsv`, `timings.tsv`, plus `bats/`, `gui/`, `journals/`,
`screenshots/`. `qci triage --latest` is where a failure starts.

**Pick gates with `qci affected --changed-from main`.** It selects whole
gates, not per-component tests — a qdterm change selects `host bats gui`,
and each selected gate runs in full (~2–3 h total). For development
feedback use the cheap loops in [Iterating](#iterating-day-to-day); the
selected gates are the acceptance bar.

**Shared host:** run **one** `full`/GUI run at a time. Runs share the
`qdistro-template` domain, the baseweed bases, and host RAM/CPU (host
ports are probed per run so multiple *users* can coexist — see
[../AGENTS.md](../AGENTS.md#shared-host-qci-rule)). Check for a live run
before starting:

```sh
systemctl --user list-units 'qci-*'
pgrep -af '[c]i/bin/qci'
virsh -c qemu:///session list --all | grep qci-
```

Launch long runs under `systemd-run --user --unit=qci-<name> ...` (or
`ci/bin/qci-tmux full`) so they survive the terminal — and **never edit a
script while a run that sources it is going**; bash reads scripts
incrementally and an edit mid-run corrupts the driver. Commit first,
then launch.

Failed disposable VMs are preserved (hibernated, usually `powered_off`
in practice — see `ci/AGENTS.md`) and named in `manifest.txt`.
`virsh -c qemu:///session start <vm>` brings it back; `qci cleanup
--dry-run` sweeps stale `qci-*` domains and never touches
`qdistro-daily*`.

## The GUI gate: a visual runner contract

Markdown scenarios (`tests/integration/permissions-gui/`,
`qdwin-noctalia/`, `workflow-gui/`, `presentation-gui/`, qdwin/qdlocker
suites) are playbooks executed by a **vision-capable coding agent**, one
disposable VM per scenario. `qci gui` renders each scenario into a prompt
under `agent-notes/` and hands it to `QCI_AGENT_CMD` (`{prompt}` is the
prompt path):

```sh
QCI_AGENT_CMD='codex --yolo exec -m gpt-5.6-luna -c model_reasoning_effort=medium --skip-git-repo-check - < {prompt}' \
QCI_AGENT_MODEL=gpt-5.6-luna \
  ci/bin/qci gui
```

Rules that make a run count:

- **The sanctioned driver is Codex with `gpt-5.6-luna` at
  `model_reasoning_effort=medium`**, pinned explicitly every run — an
  inherited host default (some hosts run Luna at `none`) silently changes
  the run's meaning. `QCI_AGENT_MODEL` must name the model whenever the
  template doesn't, or the manifest records `unknown`.
- **No `--ephemeral`.** The gate reads each attempt's Codex rollout to
  verify the driver actually *opened* the attested PNG frames; a
  pixel-dependent verdict from a driver that opened none is recorded
  `ERROR` (`agent-unviewed-verdict`). If the template sets `CODEX_HOME`,
  export the same path as `QCI_GUI_CODEX_HOME`.
- **Vision, not OCR.** Scenarios are graded by looking at screenshots —
  colour, layout, focus, the *absence* of a control. OCR (tesseract, when
  installed, runs over every attested frame as corroboration — otherwise
  the text column records `skip`) reads text only; a runner
  that cannot open images must record `ERROR`, never `PASS`/`FAIL`. A
  2026-09 run graded 113 scenario attempts through OCR text alone and its
  visual verdicts were worthless — that failure mode is why this rule
  exists.
- **Isolation is enforced, not optional.** The gate detaches controllers
  from the host display/session and sandboxes each agent in a Bubblewrap
  namespace that hides the host X11/Wayland/D-Bus sockets. Scenarios
  never run on the host; the `gui-qdwin` golden pins Pixman + 1280×800 so
  screenshots and injected input share stable coordinates.
- `QCI_GUI_RETRY=1` retries a scenario **once, on a fresh VM**, and only
  for tight infra signatures (`transport-timeout`,
  `agent-api-unreachable`, `agent-tooling`) — never a product
  `FAIL`/`ERROR`. A retried pass is always logged to `flake.tsv`.
- Opt-in lanes stay off in normal runs: `QCI_GUI_APPS=1` (third-party app
  compatibility, `qdwin/tests/apps/`), `QCI_LABWC_ADMIN_LANE=1` and
  `QCI_XWAYLAND_E2E=1` (the labwc/XWayland harness lane; `qci gui-admin`
  sets it). GUI concurrency defaults to serial (`QCI_GUI_JOBS` to
  override) — parallel full GUI stacks have produced flakes that don't
  reproduce in isolation.

To re-run one scenario or one file: `qci gui --scenario <abs path>`,
`qci bats --file <f.bats>`; against a preserved VM add `--vm <name>` (or
`qci replay <scenario> <vm>`). `ci/bin/qci-lane check` lists maintained
scenario groups (`gui-locker`, `bats-fast`, …) for focused runs.

## Building the disk image

The tester artifact is a **raw disk image**
(`qdistro-<version>-<snapshot>.raw.xz` + `.sha256`), built by kiwi inside
a builder VM — no host root, no host kiwi:

```sh
cd image/
QDISTRO_PROFILE=dev ./build-in-vm.sh   # ~30–40 min cold: clone + kiwi + xz + host-side proof
./verify.sh                          # boots the raw under qemu:///session, ~10–15 min
./verify.sh --stick                  # + USB/SecureBoot/nested/dd battery (what the gate runs)
```

- `build-in-vm.sh` clones the baked base, runs `kiwi-ng system build` +
  `result bundle` in the VM, copies the raw and `bundle/` to
  `$QDISTRO_BUILD_DIR` (default `/var/tmp/qdistro-build-<uid>` — never
  `/tmp`, the raw is 28 GiB), and proves the artifact on the host:
  checksum, `xz -t`, decompressed size, and the baked `PROFILE=` matching
  the request.
- `QDISTRO_PROFILE` is `dev` or `release` (default `release`) and selects
  passwordless-sudo dev mode and the SELinux mode; a mis-profiled build
  fails loudly rather than shipping mislabeled.
- The image runs the **same** bootstrap installer chain as a bare-metal
  install (`qdistro-bootstrap.sh`), in offline mode, and records the
  chain in `/var/lib/qdistro/bootstrap/installer-chain.state`; the
  provenance manifest is `/etc/qdistro/release` on the image.
- `iterate-kiwi.sh` pushes `config.xml`/`config.sh`/`build.sh` into a
  running builder for a fast loop; `verify-contents.sh` +
  `extract-root.sh` run the static checklist without booting.
- `qci image` (part of `full`) resolves `bundle/*.raw.xz`, extracts,
  checklists, and boot-verifies the same artifact — and checks its
  identity: the `SOURCE` commit, `config.xml` version, `snapshot.conf`
  snapshot, and the baked `PROFILE` must match the run's
  `QDISTRO_PROFILE` (default `release`, so a `dev` artifact needs
  `QDISTRO_PROFILE=dev` on the run too). A cached decompressed raw is
  verified byte-for-byte before reuse, so keep room for another 28 GiB.
  No artifact → the gate is `blocked`/`build`, never a pass;
  `QCI_SKIP_IMAGE=1` records an explicit skip for a dev `full` (refused
  under `QCI_RELEASE=1`).

The full contract — chain table, profile semantics, checklist, failure
modes — is [../image/AGENTS.md](../image/AGENTS.md). **Do not run
`verify.sh` while a builder VM is up** on the same session daemon.

## The installable track: packaging/

Alongside the raw image, `packaging/` builds the qdistro stack as signed
RPMs and a bootable **Agama installer ISO** (unattended via an OEMDRV
medium). Everything external is configurable via `packaging/env.sh` +
gitignored `env.local.sh`; the defaults float on Tumbleweed (this track
does not read `snapshot.conf`). Quickstart and the DUD variant for the
stock ISO: [../packaging/README.md](../packaging/README.md).

## Iterating day to day

- Edit in a worktree; `qci affected --changed-from main` picks the gates
  your change owes.
- Fast loops: `ci/bin/qci-host-run` for any pytest/meson/npm row;
  `qci bats --file <f>`; `qci gui --scenario <abs path>`;
  `qci-lane run <group>` for a maintained subset;
  `qci feedback qdfileman <paths>` for the qdfileman host job with a
  gate-obligation report.
- Debug a failed run from its artifacts: `qci triage --latest`, the
  preserved VM in `manifest.txt`, `vm-exec`/`vm-gui` into it, then a
  narrow `--vm` rerun. Save new evidence under the run dir, never only
  `/tmp`.
- `timings.tsv` in each run dir breaks down provision vs work seconds —
  the place to look before claiming a suite got slower.

## Tech stack

- **PyQt6** 6.5+ (not PyQt5, never PySide6 in tests). Modern Qt, better
  Wayland support.
- **Python 3.14** — the distro's interpreter; stdlib `tomllib` for config.
- **TOML** for config. No YAML, JSON, or INI for app config.
- **pytest** + **pytest-qt** for tests.
- **SIP-built bindings** where C++ libraries need Python hooks
  (qterminator's QTermWidget bindings are the pattern).
- **Qt signals/slots**, not GObject or event queues.

## Per-app repo layout

The Python apps follow one shape (qdwin is C/meson and the extensions are
npm — see the component map in [../AGENTS.md](../AGENTS.md) for each
component's actual entry points):

```
<component>/
├── <package>/            # source package
│   ├── __init__.py
│   ├── __main__.py       # entry point: python -m <app>
│   ├── window.py         # main window
│   ├── config.py         # config singleton
│   ├── plugin.py         # plugin loader + base classes
│   └── theme.py          # dark theme stylesheet
├── tests/                # pytest suite (+ component-local bats/GUI lanes)
│   ├── conftest.py       # shared fixtures + cleanup
│   └── test_*.py
├── doc/                  # man pages (groff)
├── po/                   # i18n
├── pyproject.toml
├── justfile
├── AGENTS.md             # agent guidelines — read first
├── README.md
└── LICENSE
```

`qdterm/` and `qdfileman/` name their directories after the GitHub repos;
the Python packages, binaries, desktop IDs and D-Bus names are still
`qterminator` and `qfileman`.

## AGENTS.md at each component root

Components keep their agent orientation notes in `AGENTS.md` — some at
the component root, some under `doc/` or `tests/` (the
[component map](../AGENTS.md) names each component's). Keep it under
~100 lines: project purpose (one paragraph), build/test commands, system
dependencies, architecture (file → role), test conventions, and the key
design decisions (the *why* behind unusual choices).

## Headless testing

**All tests must run without a display.** Inside the container toolchain
(or anywhere with the deps):

```bash
QT_QPA_PLATFORM=offscreen python3 -m pytest tests/ -v
```

GUI tests use `qtbot`; non-GUI tests don't need it at all.

**Shell-script tests use
[`bats`](https://github.com/bats-core/bats-core),** not ad-hoc `bash` +
manual asserts — one framework, isolated tests, TAP output, predictable
setup/teardown. Files named `*.bats`; a `.bats` file that makes no guest
call carries `# qci:host-only` in its first 40 lines and never spends a
VM.

### Two test layers

- **Unit + GUI-unit** inside a single app process, headless. Default for
  app development.
- **Full-stack integration** against the whole qdistro stack in a
  libvirt VM, driven from the host via [vm-dev-tools](vm-dev-tools.md):
  bats suites plus agent-driven markdown GUI scenarios. Complementary to
  in-process testing, not a replacement.

## pytest-qt conventions

- `qtbot.waitExposed(widget)` before interacting with a newly-shown widget.
- `qtbot.wait(ms)` to let the event loop progress when needed.
- `qtbot.mouseClick(widget, Qt.MouseButton.LeftButton)` for clicks.
- `qtbot.keyClick(widget, Qt.Key.Key_X, modifier)` for keyboard events.
- `qtbot.waitSignal(signal, timeout=...)` for signal-driven assertions.
- `qtbot.addWidget(w)` so qtbot cleans up the widget automatically.

Example (after qdterm's `tests/test_window.py`):

```python
def test_new_tab_shortcut(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    qtbot.keyClick(
        window,
        Qt.Key.Key_T,
        Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
    )
    assert window._tabs.count() == 2
```

## Fixture patterns

### `fresh_config`

Config singleton is isolated per test. Both constants are computed at
import time, so patch `CONFIG_DIR` **and** `CONFIG_FILE`, and drop the
cached singleton — the real version is qdterm's `tests/test_window.py`:

```python
@pytest.fixture(autouse=True)
def fresh_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp_path / "config.toml"))
    Config._instance = None
    yield
    Config._instance = None
```

### Resource cleanup

Apps that open PTYs, pipes, file descriptors, or subprocess handles must
include an autouse cleanup fixture:

```python
@pytest.fixture(autouse=True)
def _cleanup_after_test():
    """Free fds after every test to prevent exhaustion."""
    yield
    app = QApplication.instance()
    if app:
        for _ in range(3):
            app.processEvents()
            gc.collect()
            app.processEvents()
```

Without it, tests exhaust the system fd limit mid-suite.

## Test categories

| Category | Example | Runs |
|---|---|---|
| Pure unit | `test_config.py`, `test_cli.py`, `test_plugin.py` | Fast, no Qt. |
| GUI unit | `test_window.py`, `test_terminal.py`, `test_titlebar.py` | qtbot + offscreen. |
| Visual snapshot | `test_gui_visual.py` | Offscreen render compared to reference image. |
| Shortcut coverage | `test_shortcut_coverage.py` | Every shortcut in the keymap has a test. |
| Accessibility coverage | `test_accessibility.py` | Every dialog is machine-readable. |
| Integration | `test_integration_*.py` | Multi-window, mocked peripherals. |

**Shortcut-coverage tests are mandatory.** Every app maintains one that
enumerates declared shortcuts and asserts each has a corresponding action
wired up.

**Accessibility-coverage tests are mandatory** (see [ui](ui.md)). Every
app launches offscreen, opens each registered dialog, and asserts AT-SPI
+ qdistro UIModel tree is introspectable.

## Code conventions

- **Config singleton**, not Borg, not a DI framework.
- **Plugin discovery from filesystem**, not Python entry points. Plugin
  directory at `/etc/<app>/plugins/` (admin) and
  `~/.config/<app>/plugins/` (user). Avoids Python packaging complexity
  and makes plugins discoverable with `ls`.
- **Dark theme as default** (via stylesheet); light theme as secondary.
- **Every widget has a stable `objectName`** (required for
  machine-readable UI).

## Dependency management

- Runtime deps are the `dependencies` array in `[project]` of
  `pyproject.toml`.
- Test deps are the `test` array under `[project.optional-dependencies]`
  (pytest, pytest-qt).
- System deps (C++ libraries bound via SIP) documented in the component
  README under "Build dependencies" — and added to
  `ci/containers/host-packages.txt` so the toolchain image has them.

## Task runner — `justfile`

Use `just` (modern make replacement) for common per-app tasks:

```just
run:
    python3 -m <app-name>

test:
    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/ -v

test-fast:
    python3 -m pytest tests/test_config.py tests/test_cli.py tests/test_plugin.py -v

lint:
    ruff check <app-name> tests

format:
    ruff format <app-name> tests
```

## Lint / format

Per language, the linters used across the tree (all run by `qci lint` /
`qci host`, and worth running before a push):

- **Python** — **`ruff`** for both linting and formatting. **`mypy`**
  optional; add if typing needs are complex. Don't add black or flake8 —
  ruff covers both.
- **Bash** — **`shellcheck`**. The `qci lint` gate runs it
  warn-by-default (a missing shellcheck is a skip, not a failure); keep
  new scripts clean.
- **QML** (qdshell) — **`qmllint`** against `qdshell/.qmllint.ini` (run
  by `qdshell/scripts/ci-local.sh`, which `qci host` invokes). Host
  `qmllint` can't resolve the Quickshell `qs.*` modules, so
  `.qmllint.ini` disables the resulting
  import/unqualified-access/missing-property cascade; real type coverage
  over resolving `qs.*` types happens at runtime in the VM via
  qmltestrunner (and the gui gate).
- **bats** — `qci lint` also does a bats-syntax parse pass over every
  `*.bats` file.

## Editor / agent LSP setup (optional)

Host-side convenience only — **not** required to build, test, or run
qdistro. It wires up Language Server Protocol servers so editors and LLM
agents get diagnostics, go-to-definition, and references across the four
languages in this tree: Python, QML, Bash, C.

Language servers (install once on the host):

| Language | Server | Install |
|----------|------------------------|----------------------------------------------------|
| Python | `pyright-langserver` | `npm i -g pyright` (or `python3 -m pip install basedpyright`) |
| Bash | `bash-language-server` | `npm i -g bash-language-server` |
| C | `clangd` | `sudo zypper install clang-tools` |
| QML | `qmlls6` | ships with the Qt6 declarative tools |

`clangd` only resolves cross-file includes when it finds a
`compile_commands.json`. meson emits one — symlink it to the source root.
The daemons' `meson.build` requires qdwin's `qdistro-protocols` pkgconfig
file, so build qdwin first and export `PKG_CONFIG_PATH` inside the
container command (`qci-host-run` does not forward the host's):

```sh
ci/bin/qci-host-run bash -c 'cd qdwin && rm -rf build-qci && meson setup build-qci && meson compile -C build-qci'
ci/bin/qci-host-run bash -c 'export PKG_CONFIG_PATH="$PWD/qdwin/build-qci/meson-uninstalled" && cd daemons && meson setup build'
ln -sf build/compile_commands.json daemons/compile_commands.json
```

Claude Code does **not** auto-detect language servers — register them in
a local plugin at `~/.claude/skills/local-lsp/.claude-plugin/plugin.json`:

```json
{
  "$schema": "https://anthropic.com/claude-code/plugin.schema.json",
  "name": "local-lsp",
  "version": "0.1.0",
  "description": "Local language servers for Python, Bash, C, and QML",
  "lspServers": {
    "python": { "command": "pyright-langserver", "args": ["--stdio"],
                "extensionToLanguage": { ".py": "python", ".pyi": "python" } },
    "bash":   { "command": "bash-language-server", "args": ["start"],
                "extensionToLanguage": { ".sh": "shellscript", ".bash": "shellscript" } },
    "c":      { "command": "clangd", "args": ["--background-index"],
                "extensionToLanguage": { ".c": "c", ".h": "c" } },
    "qml":    { "command": "qmlls6",
                "extensionToLanguage": { ".qml": "qml" } }
  }
}
```

Run `/reload-plugins` (or restart) to load it. No MCP servers are needed
for qdistro work — LSP covers in-codebase intelligence, while MCP is for
external systems the repo doesn't depend on.

## Documentation

- Man pages under each component's `doc/`. At minimum: `<app>.1` (usage)
  and `<app>-config.5` (config file reference).
- `README.md` covers features, installation, runtime deps, quickstart.
- User-facing docs live in the component directory. Admin/devops docs
  stay in the root `doc/`.

## Why these specifics

- **PyQt6 over PyQt5**: better Wayland, better HiDPI, upstream-supported.
- **Rootless Podman toolchain over host deps**: one pinned environment
  for every contributor and agent; the host can't drift from what CI
  runs.
- **Offscreen platform over Xvfb**: faster, works in containers, no
  display setup.
- **`just` over `make`**: simpler, no implicit deps, recipes are just
  commands.
- **`ruff` over black+flake8**: one tool, one config, faster.
- **Filesystem plugin discovery over entry points**: no `setup.py`
  install dance for users dropping in scripts; `ls` tells you what's
  active.
- **TOML over YAML/JSON**: stdlib support, comments allowed, less
  whitespace-sensitive.
