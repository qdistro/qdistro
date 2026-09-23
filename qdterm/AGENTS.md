# CLAUDE.md

## Project

QTerminator — Qt port of the Terminator terminal emulator. PyQt6 + QTermWidget.

## Build/test commands

```bash
# Run app
python3 -m qterminator

# Run tests (needs display or offscreen)
python3 -m pytest tests/ -v
QT_QPA_PLATFORM=offscreen python3 -m pytest tests/ -v

# Run only non-GUI tests (fast)
python3 -m pytest tests/test_config.py tests/test_cli.py tests/test_plugin.py -v
```

## Dependencies

System (openSUSE): python313-PyQt6, qtermwidget-devel, libqtermwidget6-2, python313-pytest, python313-pytest-qt

QTermWidget Python bindings: built from source via sip-wheel (see README.md).

## Architecture

- `window.py` — MainWindow with QTabWidget, all keyboard shortcuts, broadcast, zoom
- `terminal.py` — TerminalWidget wrapping QTermWidget with titlebar, config, signals
- `splitter.py` — SplitContainer (recursive QSplitter)
- `config.py` — Singleton TOML config with profiles
- `plugin.py` — Plugin system with URLHandler/MenuProvider/OutputWatcher
- `layout.py` — Serialize/restore split trees
- `theme.py` — Dark theme stylesheet

## Test conventions

- GUI tests use `qtbot.waitExposed()` + `qtbot.wait()` for real event loop rendering
- Config tests use `fresh_config` fixture (tmp_path, monkeypatch CONFIG_DIR) to isolate
- All tests run headless with `QT_QPA_PLATFORM=offscreen`

## Key design decisions

- QTermWidget for terminal emulation (C++ lib, not Python)
- TOML config (stdlib tomllib)
- Qt signals/slots instead of GObject
- Config singleton (not Borg pattern)
- Plugin discovery from file system, not entry points
