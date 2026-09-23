# Guide for AI Agents

## Project Structure

```
qfileman/
├── qfileman/           # Main package
│   ├── __init__.py    # Version info
│   ├── __main__.py    # CLI entry point
│   ├── config.py      # Configuration management
│   ├── file_model.py  # File system model
│   ├── plugin.py      # Plugin system
│   ├── window.py      # Main window UI
│   └── plugins/       # Plugin directory
│       └── builtin/   # Built-in plugins
├── tests/             # Test suite
└── pyproject.toml     # Project configuration
```

## Key Classes

- `FileManagerWindow` (`window.py`): Main Qt window. Owns the menus, toolbar,
  tree-view sidebar, status bar, plugin manager, and the `SplitContainer`
  tree. Toolbar/menu actions are routed to the currently active pane.
- `FilePane` (`pane.py`): One self-contained file-browsing pane. Owns its own
  `FileModel`, navigation history, path edit, and file list. Emits
  `path_changed`, `focused`, and `status_changed`. Multiple panes can coexist
  in a window.
- `SplitContainer` (`split_container.py`): Recursive `QSplitter` that holds
  either `FilePane` leaves or nested `SplitContainer` instances. Methods:
  `add_pane`, `split(target, orientation, factory)`, `remove_pane`,
  `find_panes`. Single-child nested splitters collapse automatically after a
  removal.
- `FileModel` (`file_model.py`): Directory listing, sort, hidden-file filter,
  and FileFilter-plugin pipeline. One per pane.
- `Plugin` / `PluginManager` (`plugin.py`): MenuProvider / NavigationHook /
  FileFilter base classes plus a discovery+lifecycle manager.
- `Config` (`config.py`): TOML-based configuration singleton.

## Running Tests

```bash
# All tests
pytest tests/

# Specific test file
pytest tests/test_plugin.py

# Single test
pytest tests/test_plugin.py::test_plugin_base_class
```

## Qt Testing

- Use `QT_QPA_PLATFORM=offscreen` for headless testing
- QApplication must exist before creating Qt widgets
- Clean up Qt objects after tests to prevent fd leaks

## Keyboard Layout

QFileMan ships with a Norton/Total-Commander/Krusader-flavoured key
map. Where modern Qt/GTK and Norton conventions disagree, the action
carries both shortcuts so muscle memory from either side works.

| Key | Action | Notes |
|-----|--------|-------|
| F1 | About | |
| F2 / Shift+F6 | Rename | F2 = Windows; Shift+F6 = TC |
| F3 | Quick View | Routes through the `embedded_viewer` plugin |
| F4 | Edit | Launches `$VISUAL` / `$EDITOR`, falls back to `xdg-open` |
| F5 | Copy… | Prompts for destination, default = other pane's cwd |
| F6 | Move… | Prompts for destination, default = other pane's cwd |
| F7 / Ctrl+Shift+N | New Folder | |
| Shift+F4 | New Text File… | Creates empty file, opens editor |
| F8 / Del | Delete | Use the `trash` plugin's "Move to Trash" for the recoverable path |
| F10 / Ctrl+Q | Quit | |
| Alt+F5 | Pack… | Routes through the `archive` plugin's create flow |
| Alt+F6 | Unpack… | Routes through the `archive` plugin's extract flow |
| Alt+F7 / Ctrl+F | Find… | |
| Ctrl+R / Shift+F5 | Refresh | TC's "re-read source" |
| Ctrl+L | Folder Size… | Routes through the `folder_size` plugin |
| Ctrl+U | Swap Panes | Exchanges the cwd of the active and next pane |
| Tab | Switch Pane | |
| Alt+Up / Backspace | Parent Directory | |
| Alt+Home | Home | |
| Ctrl+Shift+L | Split Right | |
| Ctrl+Shift+D | Split Down | |
| Ctrl+W | Close Pane | |
| Ctrl+, | Preferences… | |

## Plugin Types

All three are wired into the running window:

1. **MenuProvider** — items added by `get_menu_items(file_item)` appear in the
   file context menu. Iterated in `FileManagerWindow._show_context_menu`.
2. **NavigationHook** — `on_enter_directory(path)` returning `False` blocks the
   navigation; `on_leave_directory(path)` is called after a successful move.
   Iterated in `FileManagerWindow._update_path`.
3. **FileFilter** — `filter_files(paths) -> paths` runs inside
   `FileModel._load_files` for every listing. Pushed into the model by
   `FileManagerWindow.set_plugin_manager` (which also clears any prior
   filters, so calling it twice is safe).

## Adding a New Plugin

1. Create file in `qfileman/plugins/builtin/<name>.py`
2. Define class extending appropriate base class
3. Implement required methods
4. Add tests in `tests/test_plugin.py` (or `tests/test_builtin_plugins.py`)

Files whose names start with `_` are skipped by discovery, so shared
helpers can live alongside the plugins — see `_runner.py` for the
subprocess + `QProcess` wrapper used by archive / remote_copy /
rsync_sync.

## Built-in Plugins

| Plugin         | Type          | Notes                                              |
|----------------|---------------|----------------------------------------------------|
| `bookmarks`    | MenuProvider  | Add/remove folder bookmarks                        |
| `file_info`    | MenuProvider  | Stat-based info dialog                             |
| `filter`       | FileFilter    | Include/exclude by extension                       |
| `quick_nav`    | NavigationHook| Stub example                                       |
| `archive`      | MenuProvider  | Extract/create via `tar`/`unzip`/`7z`/`unrar`      |
| `remote_copy`  | MenuProvider  | Upload via `scp`, `sftp` (batch), or `lftp`        |
| `rsync_sync`   | MenuProvider  | Resumable copy/move with `--partial --append-verify --inplace` (move = `--remove-source-files`) |
| `checksum`     | MenuProvider  | MD5 / SHA-1 / SHA-256 via `hashlib`                |
| `multi_rename` | MenuProvider  | Batch rename with `[N]`/`[E]`/`[C]`/`[C:n]` tokens |
| `diff`         | MenuProvider  | "Diff With…" + "Set/Diff Against Source" via `meld`/`kdiff3`/`diffuse`/`xxdiff`/`kompare`, plain `diff` fallback |
| `kfind`        | MenuProvider  | Launch KDE's KFind scoped to the current directory; hides itself when not installed |
| `fuzzy_search` | MenuProvider  | In-process fzf-style scorer (subsequence + word-boundary + consecutive-run bonuses), smart-case |
| `open_terminal`| MenuProvider  | Open a terminal in current dir; prefers a running QTerminator tab, then `$TERMINAL`, then a built-in priority list |
| `qterminator_link` | MenuProvider + NavigationHook | Link to a QTerminator tab; navigating in QFileMan types `cd <dir>` into the tab |
| `trash`        | MenuProvider  | Move-to-trash via `gio trash` / `trash-put` / `kioclient5` — recoverable counterpart to Delete |
| `open_with`    | MenuProvider  | "Open With…" picker built from `xdg-mime` + `mimeinfo.cache` + parsed `.desktop` files |
| `git_status`   | MenuProvider  | Inside-repo actions: Status / Diff / Log / Blame / Stage / Commit (repo root detected by walking up to `.git`) |
| `folder_size`  | MenuProvider  | Recursive `du`-style size dialog with sortable per-child breakdown |
| `mount_manager`| MenuProvider  | List block devices (`lsblk -J`); Mount / Unmount / Eject via `udisksctl` |
| `embedded_viewer` | MenuProvider | TC Lister-style Quick View — text / image / hex with auto-detection and a mode toggle |
| `rclone`       | MenuProvider  | Copy / Sync to any rclone backend (S3, Drive, Dropbox, B2, WebDAV, SFTP, …) with progress2-style updates |
| `sync_folders` | MenuProvider  | Krusader Synchronizer-style two-pane folder compare with `rsync --dry-run --itemize-changes` preview, then apply |

Plugins that shell out check `PATH` first and surface a friendly warning
if their backing tool isn't installed (e.g. `rsync`, `scp`, `7z`,
`gio`, `udisksctl`, `git`, `rclone`).

## Sibling-project integration

QFileMan ships with two integration helpers that are not plugins
themselves (they live as `_*.py` files so plugin discovery skips them)
but are wired into the runner and several plugins:

* `_qdshell.py` — posts `org.freedesktop.Notifications` D-Bus messages
  via `PyQt6.QtDBus`. Any compliant daemon receives them, including
  qdshell's Quickshell `NotificationServer`. Every `CommandDialog` in
  `_runner.py` posts *Started* on launch and replaces it with
  *Done* / *Failed* (or *Cancelled*) on exit; intermediate rsync
  `--info=progress2` percentages are surfaced with the standard
  `value` hint, throttled to one update per second.
* `_qterminator.py` — minimal JSON-RPC client for the QTerminator
  `agent_control` unix socket (`$XDG_RUNTIME_DIR/qterminator-agent-$UID.sock`).
  Wraps `list_tabs`, `attach`, `send_text`, `open_tab`, and a `cd`
  convenience that does `attach → send_text("cd <dir>\n") → detach` on
  a single connection (attach state is per-connection on the server).
  Used by `open_terminal` and `qterminator_link`.

Neither helper hard-fails when the peer isn't running; instead they
return `None` / raise `QTerminatorUnavailable` so callers can degrade
to whatever fallback makes sense (notifications: silently drop;
qterminator: fall back to spawning a standalone terminal).
