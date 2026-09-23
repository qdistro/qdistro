"""Notification and screen flash plugins.

Pattern-match notifications: flash the terminal when configured patterns
appear in output (e.g., "BUILD FAILED", your username in IRC, etc.).

Inactivity detector: flash when the foreground process appears idle
(no output for a configurable period, or process has zero CPU usage).

Configuration (config.toml):
    [plugins.notifications]
    flash_patterns = ["BUILD FAILED", "ERROR", "your-irc-nick"]
    flash_color = "#ff4444"
    flash_duration_ms = 150
    flash_count = 3
    inactivity_timeout_s = 60
    check_cpu = true
"""

import re
import time

from PyQt6.QtCore import QTimer

from qterminator.config import Config
from qterminator.plugin import OutputWatcher

DEFAULT_FLASH_COLOR = "#ff4444"
DEFAULT_FLASH_DURATION = 150
DEFAULT_FLASH_COUNT = 3
DEFAULT_INACTIVITY_TIMEOUT = 60


def _flash_terminal(terminal, color, duration_ms, count):
    """Flash the terminal background multiple times."""
    term = terminal.term
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


class PatternNotifier(OutputWatcher):
    """Flash terminal when configured patterns appear in output."""

    name = "pattern_notifier"
    description = "Flash screen on pattern match in terminal output"
    version = "1.0"
    capabilities = ["output_watcher"]

    def __init__(self):
        super().__init__()
        self._patterns = []
        self._compiled = None
        self._flash_color = DEFAULT_FLASH_COLOR
        self._flash_duration = DEFAULT_FLASH_DURATION
        self._flash_count = DEFAULT_FLASH_COUNT
        self._cooldown = {}  # terminal id -> last flash time

    def activate(self, app_controller):
        config = Config()
        patterns = config.get(
            "plugins", "notifications", "flash_patterns", default=[]
        )
        if patterns:
            self._patterns = patterns
            # Combine all patterns into one regex
            escaped = [re.escape(p) if isinstance(p, str) else p for p in patterns]
            self._compiled = re.compile('|'.join(escaped), re.IGNORECASE)

        self._flash_color = config.get(
            "plugins", "notifications", "flash_color",
            default=DEFAULT_FLASH_COLOR,
        )
        self._flash_duration = config.get(
            "plugins", "notifications", "flash_duration_ms",
            default=DEFAULT_FLASH_DURATION,
        )
        self._flash_count = config.get(
            "plugins", "notifications", "flash_count",
            default=DEFAULT_FLASH_COUNT,
        )

    def on_output(self, terminal, text):
        if not self._compiled:
            return
        if not self._compiled.search(text):
            return

        # Cooldown: don't flash more than once per 2 seconds per terminal
        tid = id(terminal)
        now = time.time()
        if now - self._cooldown.get(tid, 0) < 2.0:
            return
        self._cooldown[tid] = now

        _flash_terminal(
            terminal, self._flash_color,
            self._flash_duration, self._flash_count,
        )

        # Desktop notification
        try:
            import subprocess
            subprocess.Popen(
                ['notify-send', 'QTerminator', 'Pattern matched in terminal'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            pass


class InactivityDetector(OutputWatcher):
    """Flash terminal when process appears idle.

    Detects two conditions:
    1. No output for inactivity_timeout_s seconds
    2. Foreground process has near-zero CPU usage (optional)
    """

    name = "inactivity_detector"
    description = "Flash screen when terminal process is idle"
    version = "1.0"
    capabilities = ["output_watcher"]

    def __init__(self):
        super().__init__()
        self._last_output = {}  # terminal id -> timestamp
        self._notified = {}  # terminal id -> bool (already notified)
        self._timeout = DEFAULT_INACTIVITY_TIMEOUT
        self._check_cpu = True
        self._timer = None

    def activate(self, app_controller):
        self._app = app_controller
        config = Config()
        self._timeout = config.get(
            "plugins", "notifications", "inactivity_timeout_s",
            default=DEFAULT_INACTIVITY_TIMEOUT,
        )
        self._check_cpu = config.get(
            "plugins", "notifications", "check_cpu", default=True,
        )

        self._timer = QTimer()
        self._timer.timeout.connect(self._check_all)
        self._timer.start(5000)  # check every 5 seconds

    def deactivate(self):
        if self._timer:
            self._timer.stop()
            self._timer = None

    def on_output(self, terminal, text):
        tid = id(terminal)
        self._last_output[tid] = time.time()
        self._notified[tid] = False

    def _check_all(self):
        if not self._app or not hasattr(self._app, '_tabs'):
            return

        # ``self._app`` may be a Python reference to a QMainWindow
        # whose C++ side has been destroyed (test teardown, window
        # close before deactivate). ``hasattr`` won't detect that —
        # the next attribute call into the deleted wrapper raises
        # ``RuntimeError``. Catch and stop polling instead of
        # crashing the event loop on every tick.
        try:
            count = self._app._tabs.count()
        except RuntimeError:
            if self._timer:
                self._timer.stop()
                self._timer = None
            self._app = None
            return

        now = time.time()
        for i in range(count):
            try:
                split = self._app._tabs.widget(i)
            except RuntimeError:
                return
            if split is None:
                continue
            for term in split.find_terminals():
                self._check_terminal(term, now)

    def _check_terminal(self, terminal, now):
        tid = id(terminal)

        # Skip if already notified or no output tracked yet
        if self._notified.get(tid, False):
            return
        last = self._last_output.get(tid)
        if last is None:
            return

        # Check timeout
        elapsed = now - last
        if elapsed < self._timeout:
            return

        # Check if process has a running foreground process
        if not terminal.has_running_process():
            return

        # Optional CPU check
        if self._check_cpu and not self._process_is_idle(terminal):
            return

        # Flash and notify
        self._notified[tid] = True
        _flash_terminal(terminal, "#ffaa00", 200, 2)

        try:
            import subprocess
            subprocess.Popen(
                ['notify-send', 'QTerminator',
                 f'Process idle for {int(elapsed)}s'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            pass

    def _process_is_idle(self, terminal):
        """Check if the foreground process has near-zero CPU usage."""
        pid = terminal.foreground_pid()
        if pid <= 0:
            return False
        try:
            stat_path = f"/proc/{pid}/stat"
            with open(stat_path) as f:
                fields = f.read().split()
            # Fields 13 and 14 are utime and stime (in clock ticks)
            utime = int(fields[13])
            stime = int(fields[14])
            total = utime + stime

            # Compare with previous reading
            key = f"cpu_{pid}"
            prev = getattr(self, key, None)
            setattr(self, key, (time.time(), total))

            if prev is None:
                return False
            prev_time, prev_total = prev
            dt = time.time() - prev_time
            if dt < 1:
                return False
            cpu_ticks = total - prev_total
            # Less than 1 tick per second = essentially idle
            return cpu_ticks / dt < 1.0
        except (FileNotFoundError, IndexError, ValueError):
            return False
