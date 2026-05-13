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
qdistro/scripts/vm/spin-test-vm.sh validation-$(date +%y%m%d%H%M)
```

Integration tests run **inside the baked VM**. GUI tests must never
run on the host (see [AGENTS.md](AGENTS.md)).

For the qdshell QML stack: `cd qdshell && quickshell -p shell.qml`,
but only inside a VM session where qdwin is the active compositor.

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

- **`ruff`** for both linting and formatting.
- **`mypy`** optional; add if typing needs are complex.
- Don't add black or flake8 — ruff covers both.

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
