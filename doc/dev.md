# Dev guidelines

Patterns distilled from the existing first-party apps (qterminator,
qnotebook) and the broker / SDK / admin-app stack. These conventions are
the canonical reference — when in doubt about layout, testing, config, or
idioms, follow what's described here.

## Multi-repo dev setup

qdistro spans three repos that must be cloned side-by-side. Scripts
and tests reference siblings with hard-coded relative paths
(`../qdwin`, `../qdshell`). No env vars to set; no system install
required.

```sh
mkdir qdistro-org && cd qdistro-org
git clone https://codeberg.org/qdistro/qdistro.git
git clone https://codeberg.org/qdistro/qdwin.git
git clone https://codeberg.org/qdistro/qdshell.git
```

(The "no system install required" above is about the repo *layout* — siblings
are found by relative path, with no env vars. The build *toolchain* below still
has to be on the host.)

### Host build prerequisites

The build steps assume these tools are on `PATH`:

- **meson** + **ninja** + **pkg-config** — build the qdwin compositor and the C
  daemons.
- **Python 3** + **pytest** — headless unit tests.
- **npm** — WebExtension tests/builds.
- **SELinux policy build tools** — needed when compiling qdistro `.te` modules
  locally. On Tumbleweed:

  ```sh
  sudo zypper install selinux-policy-devel checkpolicy policycoreutils policycoreutils-devel policycoreutils-python-utils
  ```

  The qdistro SELinux module `Makefile`s call
  `/usr/share/selinux/devel/Makefile`, which is provided by
  `selinux-policy-devel`. If policy builds fail with that file missing, first
  confirm the package is installed and the path exists:

  ```sh
  rpm -q selinux-policy-devel checkpolicy policycoreutils-devel
  test -f /usr/share/selinux/devel/Makefile
  ```

  If the devel `Makefile` exists, the host has the policy build toolchain; later
  `checkmodule` errors are policy/source issues, not missing host packages.

`qdistro/ci/bin/qci preflight` checks the build/lint tools it can (meson, ninja,
pkg-config, npm, ruff, mypy) plus virsh/KVM/VM tooling, and reports any that are
missing. It records optional tools (meson included) as skip/warn rather than
failing — the hard failure surfaces later in `qci host`, where the qdwin/qdshell
`meson setup && compile` rows error out if meson isn't installed. (pytest is not
a preflight check; it runs as part of the host test step.)

> On hosts where the distro forbids a system `pip install` of meson (PEP 668),
> either install the packaged meson (e.g. `meson` 1.x from the distro) or build
> through a throwaway venv (`python -m venv` then `pip install meson`).

Build order (from the parent `qdistro-org/` directory):

```sh
# 1. Build qdwin — the compositor.
(cd qdwin && meson setup build && meson compile -C build)

# 2. Build the umbrella's C daemons against qdwin's XML.
(cd qdistro/daemons && meson setup build && meson compile -C build)

# 3. Headless unit tests.
(cd qdistro && pytest)

# 4. Bake a test VM (one-time, ~5-10 min for baseweed, ~10-25 min
#    for the dependency-baked overlay).
qdistro/scripts/vm/build-baseweed-from-scratch.sh
qdistro/scripts/vm/build-baked-baseweed.sh

# 5. Spin a fresh test VM + run the integration suite.
#    The fixed qdistro test VM password is Pa_ssw0rd45. QDWIN_VM_TEMPLATE is
#    optional — spin-test-vm.sh auto-creates a "qdistro-template"
#    libvirt domain on first run.
qdistro/scripts/vm/spin-test-vm.sh validation-$(date +%y%m%d%H%M)
```

Prerequisites for the libvirt session (set up once):

```sh
sudo zypper install libvirt qemu-kvm virt-install virt-manager bubblewrap
sudo usermod -aG libvirt $(whoami)
# Then log out / log in for the group change to take effect.
virsh -c qemu:///session list --all
```

The final command verifies the per-user libvirt session used by qdistro. If it
cannot connect, start the distro's system libvirt service/socket (or open
virt-manager) and repeat the check; `libvirtd.service` is not a user unit.

Integration tests run **inside the baked VM**. GUI tests must never
run on the host (see [AGENTS.md](AGENTS.md)).

For the qdshell QML stack: `cd qdshell && quickshell -p shell.qml`,
but only inside a VM session where qdwin is the active compositor.

### Agent-assisted GUI runner

Keep Claude Haiku as the default visual runner for the mechanical markdown
scenarios:

```sh
QCI_AGENT_CMD='claude -p "$(cat {prompt})" --dangerously-skip-permissions --model haiku' \
  qdistro/ci/bin/qci gui
```

Codex with `gpt-5.6-luna` is a verified vision-capable fallback when Haiku is
unavailable. Run it non-interactively and let qci place each attempt in its own
temporary working directory:

```sh
QCI_AGENT_CMD='codex --yolo exec -m gpt-5.6-luna --skip-git-repo-check --ephemeral - < {prompt}' \
QCI_AGENT_MODEL=gpt-5.6-luna \
  qdistro/ci/bin/qci gui --scenario tests/integration/permissions-gui/01-tui-approver-visual.md
```

`--yolo` is appropriate here only because the runner controls a disposable VM
and must invoke `virsh`, `vm-exec`, and evidence-writing commands without an
interactive approval. The scenario still fails closed unless the agent writes
an explicit passing verdict and exits zero.

The qci GUI gate enforces a host/guest boundary for every runner, including
Haiku and Luna:

- Graphical applications, compositors, dialogs, screenshots, and input run only
  in disposable VMs. The host process is a non-interactive controller.
- Before any GUI gate starts, qci detaches its controller processes from the
  host `DISPLAY`, Wayland display, desktop activation variables, askpass
  programs, browser launcher, and D-Bus session bus.
- Each markdown-scenario agent is additionally placed in a Bubblewrap mount
  namespace where `/tmp/.X11-unix`, the host Wayland sockets, and the user
  session-bus socket are hidden. The per-user libvirt sockets remain available,
  so `virsh -c qemu:///session`, `vm-exec`, and `vm-gui` can drive the guest.
- qci fails closed before provisioning when `bubblewrap` is unavailable. Do not
  work around this by running a GUI scenario directly on the workstation.

Consequently, a scenario must never call `virt-manager`, `virt-viewer`,
`remote-viewer`, `xdg-open`, or a graphical app on the host. Inspect images from
the artifact directory through the visual model; operate the GUI through the VM
helpers named in the scenario.

## Tech stack

- **PyQt6** 6.5+ (not PyQt5). Modern Qt, better Wayland support.
- **Python 3.11+** for stdlib `tomllib`.
- **TOML** for config. No YAML, JSON, or INI for app config.
- **pytest** + **pytest-qt** for tests.
- **SIP-built bindings** where C++ libraries need Python hooks
 (qterminator's QTermWidget bindings are the pattern).
- **Qt signals/slots**, not GObject or event queues.

## Repo layout

```
<app-name>/
├── <app-name>/ # source package
│ ├── __init__.py
│ ├── __main__.py # entry point: python -m <app>
│ ├── window.py # main window
│ ├── config.py # config singleton
│ ├── plugin.py # plugin loader + base classes
│ ├── theme.py # dark theme stylesheet
│ └── ...
├── tests/ # pytest suite
│ ├── conftest.py # shared fixtures + cleanup
│ ├── test_cli.py
│ ├── test_config.py
│ ├── test_window.py # GUI tests (qtbot)
│ ├── test_plugin.py
│ ├── test_shortcut_coverage.py
│ └── ...
├── doc/ # man pages (groff format)
│ ├── <app>.1 # usage
│ └── <app>-config.5 # config reference
├── po/ # i18n translations
├── icons/
├── pyproject.toml # package metadata
├── justfile # task runner
├── AGENTS.md # LLM-agent guidelines
├── README.md
└── LICENSE
```

## AGENTS.md at repo root

Every app has an `AGENTS.md` at its repo root — the file LLM agents read
first to orient. Keep it under 100 lines. Contents:

- Project purpose (one paragraph).
- Build / test commands.
- System dependencies.
- Architecture overview (file → role).
- Test conventions.
- Key design decisions (why we made unusual choices).

## Headless testing

**All tests must run without a display.** Standard invocation:

```bash
QT_QPA_PLATFORM=offscreen python3 -m pytest tests/ -v
```

GUI tests use `qtbot`; non-GUI tests don't need it at all.

**Shell-script tests use [`bats`](https://github.com/bats-core/bats-core),**
not ad-hoc `bash` + manual asserts. Same rationale as picking pytest for
Python: one framework, isolated tests, TAP output, predictable
setup/teardown. Files named `*.bats`, packaged on Tumbleweed as `bats`.

### Two test layers

- **Unit + GUI-unit** inside a single app process, headless. Default for
 app development.
- **Full-stack integration** against the whole qdistro stack running in a
 virt-manager VM, driven from the host via
 [vm-dev-tools](vm-dev-tools.md). Validates broker + SDK + admin
 approval app end-to-end, takes real screenshots, simulates admin
 clicking Approve. Complementary to in-process testing, not a
 replacement.

## pytest-qt conventions

- `qtbot.waitExposed(widget)` before interacting with a newly-shown widget.
- `qtbot.wait(ms)` to let the event loop progress when needed.
- `qtbot.mouseClick(widget, Qt.MouseButton.LeftButton)` for clicks.
- `qtbot.keyClick(widget, Qt.Key.Key_X, modifier)` for keyboard events.
- `qtbot.waitSignal(signal, timeout=...)` for signal-driven assertions.
- `qtbot.addWidget(w)` so qtbot cleans up the widget automatically.

Example:

```python
def test_new_tab_shortcut(qtbot):
 window = MainWindow()
 qtbot.addWidget(window)
 qtbot.waitExposed(window)
 qtbot.keyClick(
 window,
 Qt.Key.Key_T,
 Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
 )
 assert window.tabs.count() == 2
```

## Fixture patterns

### `fresh_config`

Config singleton is isolated per test — point `CONFIG_DIR` at `tmp_path`:

```python
@pytest.fixture
def fresh_config(tmp_path, monkeypatch):
 monkeypatch.setattr("<app>.config.CONFIG_DIR", tmp_path)
 config.reset()
 yield config
 config.reset()
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
|-------------------------|-----------------------------------------------------------|-----------------------------------------------|
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

## Dependencies management

- Runtime deps in `pyproject.toml` under `[project.dependencies]`.
- Test deps under `[project.optional-dependencies.test]` (pytest,
 pytest-qt).
- System deps (C++ libraries bound via SIP) documented in README under
 "Build dependencies."

## Task runner — `justfile`

Use `just` (modern make replacement) for common tasks:

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
`qci host`, and worth running locally before a push):

- **Python** — **`ruff`** for both linting and formatting. **`mypy`** optional;
  add if typing needs are complex. Don't add black or flake8 — ruff covers both.
- **Bash** — **`shellcheck`**. The `qci lint` gate runs it warn-by-default (a
  missing shellcheck is a skip, not a failure); keep new scripts clean.
- **QML** (qdshell) — **`qmllint`** against `qdshell/.qmllint.ini` (run by
  `qdshell/scripts/ci-local.sh`, which `qci host` invokes). Host `qmllint`
  can't resolve the Quickshell `qs.*` modules, so `.qmllint.ini` disables the
  resulting import/unqualified-access/missing-property cascade; real type
  coverage over resolving `qs.*` types happens at runtime in the VM via
  qmltestrunner (and the gui gate).
- **bats** — `qci lint` also does a bats-syntax parse pass over every
  `*.bats` file.

## Editor / agent LSP setup (optional)

Host-side convenience only — **not** required to build, test, or run
qdistro. It wires up Language Server Protocol servers so editors and LLM
agents (Claude Code, etc.) get diagnostics, go-to-definition, and
references across the four languages in this tree: Python, QML, Bash, C.

Language servers (install once on the host):

| Language | Server                 | Install                                            |
|----------|------------------------|----------------------------------------------------|
| Python   | `pyright-langserver`   | `npm i -g pyright` (or `pip install basedpyright`)  |
| Bash     | `bash-language-server` | `npm i -g bash-language-server`                     |
| C        | `clangd`               | `sudo zypper install clang-tools`                   |
| QML      | `qmlls6`               | ships with the Qt6 declarative tools               |

`clangd` only resolves cross-file includes when it finds a
`compile_commands.json`. meson emits one — symlink it to the source root:

```sh
(cd daemons && meson setup build)        # writes build/compile_commands.json
ln -sf build/compile_commands.json daemons/compile_commands.json
```

### Claude Code

Claude Code does **not** auto-detect language servers — register them in a
local plugin at `~/.claude/skills/local-lsp/.claude-plugin/plugin.json`:

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

Run `/reload-plugins` (or restart) to load it; verify with
`claude plugin list`. No MCP servers are needed for qdistro work — LSP
covers in-codebase intelligence, while MCP is for external systems
(databases, issue trackers) the repo doesn't depend on.

## Documentation

- Man pages under `doc/`. At minimum: `<app>.1` (usage) and
 `<app>-config.5` (config file reference).
- `README.md` covers features, installation, runtime deps, quickstart.
- User-facing docs live in the app repo. Admin/devops docs stay in the
 umbrella qdistro repo.

## Why these specifics

- **PyQt6 over PyQt5**: better Wayland, better HiDPI, upstream-supported.
- **Offscreen platform over Xvfb**: faster, works in containers, no
 display setup.
- **`just` over `make`**: simpler, no implicit deps, recipes are just
 commands.
- **`ruff` over black+flake8**: one tool, one config, faster.
- **Filesystem plugin discovery over entry points**: no `setup.py` install
 dance for users dropping in scripts; `ls` tells you what's active.
- **TOML over YAML/JSON**: stdlib support, comments allowed, less
 whitespace-sensitive.
