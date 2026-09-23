# QTerminator

A Qt port of the [Terminator](https://github.com/gnome-terminator/terminator) terminal emulator, built with PyQt6 and QTermWidget.

## Role in qdistro

qterminator is the first-party terminal for qdistro. It gives the owner a
modifiable PyQt terminal with tabs, recursive splits, profiles, plugins, and
agent-control hooks, while fitting the same testing/configuration conventions as
qdbrowser, qnotebook, and qfileman.

In qdistro images, qterminator depends on the SIP-built QTermWidget Python
binding. Source installs must build or install that binding before the terminal
is considered usable.

## Features

- **Tabs and splits** -- horizontal and vertical splits with recursive nesting; tab bar with close, rename, and reorder
- **Context menu** -- right-click menu for splits, tabs, grouping, profiles, plugins, and more
- **Per-terminal titlebar** -- shows title, group indicator, read-only badge, and activity status
- **Terminal grouping** -- assign terminals to named groups; broadcast input to a group or all terminals
- **Broadcast input** -- type once, send to multiple terminals simultaneously
- **Read-only mode** -- lock terminal input to prevent accidental keystrokes
- **Zoom and maximize** -- per-terminal zoom in/out/reset and toggle maximize of a single terminal pane
- **Full screen** -- F11 full-screen toggle
- **Dark theme** -- consistent dark palette and stylesheet across all widgets
- **Preferences dialog** -- font, color scheme (13 built-in), cursor shape, scrollback, tab position, and more
- **Config persistence** -- TOML config at `~/.config/qterminator/config.toml`
- **Layout save/restore** -- splits and tabs restored automatically on next launch
- **Profile system** -- named profiles with per-terminal switching and cycling
- **Plugin system** -- URLHandler, MenuProvider, and OutputWatcher base classes; user and built-in plugin directories
- **URL handlers** -- detect and open HTTP, file, and email URLs from terminal output
- **CLI arguments** -- working directory, title, execute command, geometry, no-restore
- **Activity monitor** -- watch terminals for new output or prolonged silence
- **Scrollback navigation** -- Shift+PgUp/PgDown for scrollback
- **Visual bell** -- visual flash on terminal bell
- **Keyboard shortcuts** -- bindable shortcuts for every common operation (see the tables below)
- **Agent control** -- `qterminator-mcp` exposes the terminal to coding agents
  as an MCP (Model Context Protocol) server; `qterminator-shell-integration`
  installs the shell-side hooks

## Installation

### Runtime dependencies

| Package | Required | Purpose |
|---|---|---|
| Python 3.10+ | yes | Runtime (TOML parser needs 3.11+, else `tomli` package) |
| PyQt6 6.5+ | yes | Qt bindings |
| QTermWidget 6 | yes | Terminal emulation library (C++) |
| QTermWidget Python bindings | yes | SIP-built Python bindings (see below) |
| `tomli` | only Python <3.11 | TOML parser fallback |
| `setproctitle` | optional | Better process name in `ps`/`top` (without it, falls back to Linux `prctl` for the comm name only) |
| `tmux` | optional | For the `tmux_integration` plugin |
| `notify-send` (libnotify) | optional | Desktop notifications from the `notifications` and `file_monitor` plugins |
| `xdg-open` | optional | Opening URLs and files via `pattern_links` plugin |

### Build dependencies (only if building QTermWidget Python bindings)

| Package | Purpose |
|---|---|
| GCC C++ compiler | Build the SIP wheel |
| CMake | QTermWidget build system |
| qmake6 | Qt build tool |
| sip / pyqt-builder | Generate Python bindings |
| QTermWidget development headers | C++ headers for binding generation |

### System packages by distribution

#### openSUSE Tumbleweed

openSUSE prefixes Python packages with `pythonNNN-` matching the
default Python version. The justfile detects this automatically; for
manual install, replace `pythonXY` with your default (e.g., `python313`).

```bash
PY="python$(python3 -c 'import sys; print(f"{sys.version_info.major}{sys.version_info.minor}")')"

sudo zypper install \
    $PY-PyQt6 $PY-PyQt6-devel \
    qtermwidget-devel libqtermwidget6-2 qtermwidget-data \
    gcc-c++ cmake \
    $PY-pyqt-builder \
    $PY-pytest $PY-pytest-qt

# Optional but recommended
sudo zypper install $PY-setproctitle libnotify-tools tmux xdg-utils
```

#### Ubuntu 24.04 / Debian 13

```bash
sudo apt update && sudo apt install -y \
    python3-pyqt6 python3-pyqt6.qtsvg \
    libqtermwidget6-3 libqtermwidget6-3-dev qtermwidget-data \
    g++ cmake qmake6 \
    python3-pyqt-builder sip-tools \
    python3-pytest python3-pytest-qt

# Optional
sudo apt install -y python3-setproctitle libnotify-bin tmux xdg-utils
```

#### Fedora 40+

```bash
sudo dnf install -y \
    python3-pyqt6 python3-pyqt6-devel \
    qtermwidget-qt6-devel qtermwidget-qt6 \
    gcc-c++ cmake qt6-qtbase-devel \
    python3-pyqt-builder python3-sip-devel \
    python3-pytest python3-pytest-qt

# Optional
sudo dnf install -y python3-setproctitle libnotify tmux xdg-utils
```

### QTermWidget SIP binding build

The Python bindings for QTermWidget are not shipped by any distro and
must be built from source. The vendored upstream source lives in
`qtermwidget-pyqt/` so no network is required:

```bash
just build-sip
```

Or directly: `util/build-sip.sh`. The script auto-detects PyQt6 paths
and qtermwidget headers across distros (openSUSE, Ubuntu, Fedora).

Verify:

```bash
just verify
# or: python3 -c "from QTermWidget import QTermWidget; print('OK')"
```

To refresh the vendored source from upstream:

```bash
just update-vendor          # latest 2.3.0 tag
just update-vendor 2.4.0    # specific version
```

## Usage

```bash
python3 -m qterminator
```

### CLI options

```
python3 -m qterminator [OPTIONS]

  -d, --working-directory DIR   Set the initial working directory
  -T, --title TITLE             Set the window title
  -e, --execute CMD...          Execute a command instead of a shell
  --geometry WxH                Set window size (e.g. 1024x768)
  --no-restore                  Do not restore the previous session layout
  --version                     Show version and exit
```

## Configuration

Config file location: `~/.config/qterminator/config.toml`

### General settings

```toml
[general]
tab_position = "top"       # top, bottom, left, right
confirm_close = true
show_menubar = false
```

### Profiles

```toml
[profiles.default]
font_family = "Monospace"
font_size = 11
color_scheme = "Linux"
cursor_shape = "block"     # block, underline, ibeam
cursor_blink = true
scrollback_lines = 5000
scroll_on_keystroke = true
copy_on_selection = false
exit_action = "close"      # close, restart, hold
```

Additional profiles can be created by adding new sections:

```toml
[profiles.light]
font_family = "Source Code Pro"
font_size = 12
color_scheme = "WhiteOnBlack"
```

### Keybinding overrides

```toml
[keybindings]
new_tab = "Ctrl+Shift+T"
split_horizontal = "Ctrl+Shift+O"
# See config.py for the full list of bindable actions
```

## Keyboard Shortcuts

### Tabs

| Action | Shortcut |
|---|---|
| New Tab | Ctrl+Shift+T |
| Close Terminal | Ctrl+Shift+W |
| Next Tab | Ctrl+PgDown |
| Previous Tab | Ctrl+PgUp |
| Move Tab Left | Ctrl+Shift+PgUp |
| Move Tab Right | Ctrl+Shift+PgDown |
| Switch to Tab 1--9 | Alt+1 through Alt+9 |
| Cycle Next Terminal | Ctrl+Tab |
| Cycle Previous Terminal | Ctrl+Shift+Tab |

### Splits

| Action | Shortcut |
|---|---|
| Split Horizontally | Ctrl+Shift+O |
| Split Vertically | Ctrl+Shift+E |
| Rotate Splits | Super+R |
| Navigate Left | Alt+Left |
| Navigate Right | Alt+Right |
| Navigate Up | Alt+Up |
| Navigate Down | Alt+Down |
| Resize Split Left | Ctrl+Shift+Left |
| Resize Split Right | Ctrl+Shift+Right |
| Resize Split Up | Ctrl+Shift+Up |
| Resize Split Down | Ctrl+Shift+Down |

### View

| Action | Shortcut |
|---|---|
| Maximize Terminal | Ctrl+Shift+Z |
| Full Screen | F11 |
| Zoom In | Ctrl+Shift+= |
| Zoom Out | Ctrl+Shift+- |
| Zoom Normal | Ctrl+0 |
| Toggle Scrollbar | Ctrl+Shift+S |
| Toggle Menu Bar | Ctrl+Shift+M |

### Edit

| Action | Shortcut |
|---|---|
| Copy | Ctrl+Shift+C |
| Paste | Ctrl+Shift+V |
| Search | Ctrl+Shift+F |
| Reset | Ctrl+Shift+R |
| Reset and Clear | Ctrl+Shift+G |

### Terminal

| Action | Shortcut |
|---|---|
| Edit Terminal Title | Ctrl+Alt+X |
| Edit Tab Title | Ctrl+Alt+A |
| Edit Window Title | Ctrl+Alt+W |
| Next Profile | Ctrl+Alt+N |
| Previous Profile | Ctrl+Alt+P |
| Scroll Page Up | Shift+PgUp |
| Scroll Page Down | Shift+PgDown |

### Window

| Action | Shortcut |
|---|---|
| New Window | Ctrl+Shift+I |
| Quit | Ctrl+Shift+Q |

## Plugin System

QTerminator supports plugins that extend terminal functionality. Plugins are Python files placed in either:

- `~/.config/qterminator/plugins/` (user plugins)
- `qterminator/plugins/` (built-in plugins)

Bundled plugins auto-load at startup. User plugins are trusted Python code and
must be explicitly enabled in `~/.config/qterminator/config.toml` before they
auto-activate:

```toml
[plugins.my_plugin]
enabled = true
```

### Plugin base classes

There are three base classes in `qterminator.plugin`:

**URLHandler** -- matches patterns in terminal output and opens URLs.

```python
from qterminator.plugin import URLHandler

class JiraHandler(URLHandler):
    name = "jira_handler"
    match_pattern = r'\bJIRA-\d+\b'

    def handle_url(self, url):
        import webbrowser
        webbrowser.open(f"https://jira.example.com/browse/{url}")
        return url
```

**MenuProvider** -- adds items to the right-click context menu.

```python
from qterminator.plugin import MenuProvider

class MyMenu(MenuProvider):
    name = "my_menu"

    def get_menu_items(self, terminal):
        return [("Run htop", lambda: terminal.send_text("htop\n"))]
```

**OutputWatcher** -- reacts to new terminal output.

```python
from qterminator.plugin import OutputWatcher

class ErrorAlert(OutputWatcher):
    name = "error_alert"

    def on_output(self, terminal, text):
        if "ERROR" in text:
            print(f"Error detected in {terminal.title()}")
```

### Built-in plugins

- `url_handlers` -- detects HTTP, file, and email URLs
- `custom_commands` -- user-defined commands from config
- `logger` -- logs terminal output to files
- `terminal_screenshot` -- saves terminal content as PNG

## Development

### Running tests

```bash
just test        # per-file subprocess isolation, offscreen (preferred)
just lint        # ruff

# Or directly:
QT_QPA_PLATFORM=offscreen python3 -m pytest tests/ -v
```

The test suite covers configuration, CLI parsing, context menus, GUI
rendering, layout serialization, the plugin system, preferences, terminal
widgets, and window management.

### Project structure

The `qterminator/` package holds the app: entry point and CLI parsing
(`__main__.py`), the TOML config system (`config.py`), the terminal widget and
recursive splitter (`terminal.py`, `splitter.py`), window/tab/shortcut
management (`window.py`), the plugin manager and base classes (`plugin.py`),
the MCP server and shell integration (`mcp_server.py`,
`shell_integration_cli.py`), and built-in plugins under
`qterminator/plugins/`. Tests live in `tests/`, one `test_*.py` per area,
mirroring the module names.

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE) (GPL-3.0-only).
