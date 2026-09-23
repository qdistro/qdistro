"""File modification monitor plugin.

Watches the current working directory of each terminal. When files in the
directory stop being modified for a configured amount of time, flashes the
terminal and sends a desktop notification.

Typical use case: you kick off a build or test run that writes log files to
the cwd. When the writes stop, you get notified that the build is done --
even if the process keeps running or output went to a log file you aren't
tailing.

Configuration (config.toml):

    [plugins.file_monitor]
    enabled = true
    inactivity_timeout_s = 30     # notify after this many seconds idle
    recursive = false             # walk subdirectories
    check_interval_s = 5          # polling interval
    ignore_patterns = ["*.tmp", "*.swp", ".git"]
"""

import fnmatch
import os
import subprocess
import time

from PyQt6.QtCore import QTimer

from qterminator.config import Config
from qterminator.plugin import Plugin

try:
    from qterminator.plugins.notifications import _flash_terminal
except Exception:  # pragma: no cover - fallback if import fails
    def _flash_terminal(terminal, color, duration_ms, count):
        term = getattr(terminal, "term", None)
        if term is None:
            return
        original = term.styleSheet()

        def do_flash(remaining):
            if remaining <= 0:
                term.setStyleSheet(original)
                return
            term.setStyleSheet(f"background-color: {color};")
            QTimer.singleShot(duration_ms, lambda: _off(remaining))

        def _off(remaining):
            term.setStyleSheet(original)
            QTimer.singleShot(duration_ms, lambda: do_flash(remaining - 1))

        do_flash(count)


DEFAULT_INACTIVITY_TIMEOUT = 30
DEFAULT_CHECK_INTERVAL = 5
DEFAULT_RECURSIVE = False
DEFAULT_IGNORE_PATTERNS = ["*.tmp", "*.swp", ".git"]
FLASH_COLOR = "#44aaff"
FLASH_DURATION_MS = 200
FLASH_COUNT = 2


def _matches_any(name, patterns):
    """Return True if name matches any fnmatch pattern."""
    for pat in patterns:
        if fnmatch.fnmatch(name, pat):
            return True
    return False


class FileMonitorPlugin(Plugin):
    """Monitors file modification times in terminal cwd."""

    name = "file_monitor"
    description = "Notify when file activity stops in cwd"
    version = "1.0"
    capabilities = ["file_monitor"]

    def __init__(self):
        super().__init__()
        self._app = None
        self._timer = None
        self._inactivity_timeout = DEFAULT_INACTIVITY_TIMEOUT
        self._check_interval = DEFAULT_CHECK_INTERVAL
        self._recursive = DEFAULT_RECURSIVE
        self._ignore_patterns = list(DEFAULT_IGNORE_PATTERNS)
        # Per-terminal state: id(terminal) -> dict
        #   last_max_mtime: float | None
        #   last_change_time: float  (wall-clock time we last saw a change)
        #   notified: bool
        #   last_path: str | None    (detect cwd change)
        self._state = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def activate(self, app_controller):
        self._app = app_controller
        config = Config()
        self._inactivity_timeout = config.get(
            "plugins", "file_monitor", "inactivity_timeout_s",
            default=DEFAULT_INACTIVITY_TIMEOUT,
        )
        self._check_interval = config.get(
            "plugins", "file_monitor", "check_interval_s",
            default=DEFAULT_CHECK_INTERVAL,
        )
        self._recursive = config.get(
            "plugins", "file_monitor", "recursive",
            default=DEFAULT_RECURSIVE,
        )
        self._ignore_patterns = config.get(
            "plugins", "file_monitor", "ignore_patterns",
            default=list(DEFAULT_IGNORE_PATTERNS),
        )

        self._timer = QTimer()
        self._timer.timeout.connect(self._check_all)
        self._timer.start(int(self._check_interval * 1000))

    def deactivate(self):
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._state.clear()

    # ------------------------------------------------------------------
    # Core scanning
    # ------------------------------------------------------------------
    def _scan_directory(self, path, recursive=False, ignore_patterns=None):
        """Return the largest mtime among files under `path`, or None.

        Entries whose name matches one of `ignore_patterns` are skipped.
        When `recursive` is True the walk descends into subdirectories.
        Returns None if the directory is empty or does not exist.
        """
        if ignore_patterns is None:
            ignore_patterns = []
        if not path or not os.path.isdir(path):
            return None

        max_mtime = None

        if recursive:
            try:
                walker = os.walk(path)
            except OSError:
                return None
            for root, dirs, files in walker:
                # Prune ignored directories in place
                dirs[:] = [
                    d for d in dirs if not _matches_any(d, ignore_patterns)
                ]
                for fname in files:
                    if _matches_any(fname, ignore_patterns):
                        continue
                    full = os.path.join(root, fname)
                    try:
                        mtime = os.path.getmtime(full)
                    except OSError:
                        continue
                    if max_mtime is None or mtime > max_mtime:
                        max_mtime = mtime
        else:
            try:
                entries = list(os.scandir(path))
            except OSError:
                return None
            for entry in entries:
                if _matches_any(entry.name, ignore_patterns):
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                try:
                    mtime = entry.stat(follow_symlinks=False).st_mtime
                except OSError:
                    continue
                if max_mtime is None or mtime > max_mtime:
                    max_mtime = mtime

        return max_mtime

    # ------------------------------------------------------------------
    # Per-terminal check
    # ------------------------------------------------------------------
    def _get_cwd(self, terminal):
        """Extract cwd from a terminal widget, returning None on failure."""
        getter = getattr(terminal, "working_directory", None)
        if getter is None:
            return None
        try:
            return getter()
        except Exception:
            return None

    def _check_terminal(self, terminal, now=None):
        if now is None:
            now = time.time()

        path = self._get_cwd(terminal)
        if not path:
            return

        tid = id(terminal)
        state = self._state.setdefault(tid, {
            "last_max_mtime": None,
            "last_change_time": now,
            "notified": False,
            "last_path": None,
        })

        # If cwd changed, reset state for this terminal.
        if state["last_path"] != path:
            state["last_path"] = path
            state["last_max_mtime"] = None
            state["last_change_time"] = now
            state["notified"] = False

        max_mtime = self._scan_directory(
            path, self._recursive, self._ignore_patterns,
        )

        prev_mtime = state["last_max_mtime"]

        if max_mtime != prev_mtime:
            # Activity detected: either new files, or newer mtime.
            state["last_max_mtime"] = max_mtime
            state["last_change_time"] = now
            state["notified"] = False
            return

        # No change since last scan. Need prior activity (non-None mtime)
        # before we consider firing.
        if max_mtime is None:
            return
        if state["notified"]:
            return

        idle_for = now - state["last_change_time"]
        if idle_for < self._inactivity_timeout:
            return

        # Fire notification
        state["notified"] = True
        self._notify(terminal, path, idle_for)

    def _notify(self, terminal, path, idle_for):
        try:
            _flash_terminal(
                terminal, FLASH_COLOR, FLASH_DURATION_MS, FLASH_COUNT,
            )
        except Exception:
            pass

        try:
            subprocess.Popen(
                [
                    'notify-send', 'QTerminator',
                    f'File activity idle {int(idle_for)}s in {path}',
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------
    # Timer callback
    # ------------------------------------------------------------------
    def _check_all(self):
        app = self._app
        if app is None or not hasattr(app, "_tabs"):
            return
        # ``app._tabs`` may wrap a destroyed C++ QTabWidget after the
        # window is closed but before ``deactivate`` runs (test
        # teardown, async close). Catch the RuntimeError and stop
        # polling instead of crashing the event loop every tick.
        try:
            tabs = app._tabs
            count = tabs.count()
        except RuntimeError:
            if self._timer is not None:
                self._timer.stop()
                self._timer = None
            self._app = None
            return
        now = time.time()
        for i in range(count):
            try:
                split = tabs.widget(i)
            except RuntimeError:
                return
            finder = getattr(split, "find_terminals", None)
            if finder is None:
                continue
            for term in finder():
                self._check_terminal(term, now)
