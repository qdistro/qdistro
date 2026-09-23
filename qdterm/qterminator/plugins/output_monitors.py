"""Output monitoring plugins for QTerminator.

OutputWatcher plugins that react to terminal output by updating
titlebar indicators, sending desktop notifications, and tracking
patterns of interest.
"""

# Copyright (C) 2026 Jan Kotek
# SPDX-License-Identifier: GPL-3.0-only

import re
import subprocess
import time
from collections import deque

from PyQt6.QtCore import QTimer

from qterminator.plugin import OutputWatcher


def _install_connect_hook(app_controller, hook):
    """Register a hook to be called for each newly connected terminal.

    All _ShadowWatcher subclasses share a single wrapper around
    _connect_terminal so that activation/deactivation order doesn't
    matter and no wrapper chain can be corrupted.
    """
    hooks = getattr(app_controller, "_shadow_connect_hooks", None)
    if hooks is None:
        orig = getattr(app_controller, "_connect_terminal", None)
        if orig is None:
            return
        hooks = []
        app_controller._shadow_connect_hooks = hooks
        app_controller._shadow_connect_orig = orig

        def _dispatching_connect(terminal, _hooks=hooks, _orig=orig):
            _orig(terminal)
            for h in list(_hooks):
                h(terminal)

        app_controller._connect_terminal = _dispatching_connect
    hooks.append(hook)


def _remove_connect_hook(app_controller, hook):
    hooks = getattr(app_controller, "_shadow_connect_hooks", None)
    if hooks is None:
        return
    try:
        hooks.remove(hook)
    except ValueError:
        pass
    if not hooks:
        orig = getattr(app_controller, "_shadow_connect_orig", None)
        if orig is not None:
            app_controller._connect_terminal = orig
        try:
            del app_controller._shadow_connect_hooks
            del app_controller._shadow_connect_orig
        except AttributeError:
            pass


class _ShadowWatcher(OutputWatcher):
    """Attach an OutputWatcher to ShadowScreenRegistry for every terminal."""

    DEBOUNCE_MS = 50
    USE_RAW_OUTPUT = False

    def __init__(self):
        super().__init__()
        self._window = None
        self._handles = {}
        self._pending = set()

    def activate(self, app_controller):
        self._window = app_controller
        if getattr(app_controller, "shadow_screens", None) is None:
            return
        tabs = getattr(app_controller, "_tabs", None)
        if tabs is not None:
            for i in range(tabs.count()):
                split = tabs.widget(i)
                for terminal in split.find_terminals():
                    self._attach_terminal(terminal)
        _install_connect_hook(app_controller, self._attach_terminal)

    def deactivate(self):
        for handle, listener in list(self._handles.values()):
            try:
                handle.remove_listener(listener)
            except Exception:
                pass
            handle.release()
        self._handles.clear()
        if self._window is not None:
            _remove_connect_hook(self._window, self._attach_terminal)
        self._window = None

    def _attach_terminal(self, terminal):
        tid = id(terminal)
        if tid in self._handles or self._window is None:
            return
        registry = getattr(self._window, "shadow_screens", None)
        if registry is None:
            return
        handle = registry.acquire(terminal)

        def listener(_seq, raw, term=terminal):
            self._on_shadow_data(term, raw)

        handle.add_listener(listener)
        self._handles[tid] = (handle, listener)

    def _on_shadow_data(self, terminal, raw):
        if self.USE_RAW_OUTPUT:
            self.on_output(terminal, raw.decode("utf-8", errors="replace"))
        tid = id(terminal)
        if tid in self._pending:
            return
        self._pending.add(tid)
        QTimer.singleShot(self.DEBOUNCE_MS, lambda t=terminal: self._snapshot_fire(t))

    def _snapshot_fire(self, terminal):
        tid = id(terminal)
        self._pending.discard(tid)
        entry = self._handles.get(tid)
        if entry is None:
            return
        handle, _listener = entry
        try:
            snap = handle.snapshot()
        except Exception:
            return
        self.on_snapshot(terminal, snap)

    def on_snapshot(self, terminal, snapshot):
        pass


def _snapshot_text(snapshot) -> str:
    return "\n".join(str(line).rstrip() for line in snapshot.get("lines", []))


# ---------------------------------------------------------------------------
# 1. ErrorDetector
# ---------------------------------------------------------------------------

class ErrorDetector(_ShadowWatcher):
    """Watches for error patterns in terminal output and flags the titlebar."""

    name = "error_detector"
    description = "Detects ERROR, FATAL, FAIL, Traceback, panic, segfault in output"
    version = "1.0"
    capabilities = ["output_watcher"]

    _ERROR_PATTERN = re.compile(
        r'\b(?:ERROR|FATAL|FAIL(?:ED|URE)?|Traceback \(most recent call last\)'
        r'|panic:|segfault|Segmentation fault)',
        re.IGNORECASE,
    )

    def __init__(self):
        super().__init__()
        self._triggered = set()  # track terminals already flagged

    def on_output(self, terminal, text):
        # Direct-call compatibility for tests and legacy plugin dispatch.
        if self._ERROR_PATTERN.search(text):
            self._flag_terminal(terminal)

    def on_snapshot(self, terminal, snapshot):
        if self._ERROR_PATTERN.search(_snapshot_text(snapshot)):
            self._flag_terminal(terminal)

    def _flag_terminal(self, terminal):
        tid = id(terminal)
        try:
            titlebar = terminal._titlebar
            titlebar.set_activity(True)
            titlebar._activity_label.setStyleSheet(
                "color: #e74c3c; font-size: 10px;"  # red
            )
            titlebar._activity_label.setToolTip("Error detected in output")
            # Prefix the current title with an error marker if not already
            if tid not in self._triggered:
                current = titlebar._title_label.text()
                if not current.startswith("\u26a0 "):
                    titlebar.set_title(f"\u26a0 {current}")
                self._triggered.add(tid)
        except AttributeError:
            pass  # terminal may not have titlebar in tests


# ---------------------------------------------------------------------------
# 2. BuildProgressMonitor
# ---------------------------------------------------------------------------

class BuildProgressMonitor(_ShadowWatcher):
    """Extracts build progress percentages and shows them in the titlebar."""

    name = "build_progress"
    description = "Shows build progress (e.g. [42/100], 45%) in the titlebar"
    version = "1.0"
    capabilities = ["output_watcher"]

    # Matches patterns like [42/100], (3/10), Step 3/10
    _FRACTION_PATTERN = re.compile(
        r'(?:\[|(?:Step\s+)|\()(\d+)\s*/\s*(\d+)(?:\]|\))?'
    )
    # Matches patterns like 42%, 100%
    _PERCENT_PATTERN = re.compile(r'(\d{1,3})%')

    _COMPLETE_PATTERN = re.compile(
        r'\b(?:BUILD SUCCESS|Build complete|build successful)\b',
        re.IGNORECASE,
    )

    def __init__(self):
        super().__init__()
        self._tracking = {}  # terminal id -> last percentage

    def on_output(self, terminal, text):
        # Direct-call compatibility; activated plugin path uses snapshots.
        self._process_text(terminal, text)

    def on_snapshot(self, terminal, snapshot):
        lines = [line.rstrip() for line in snapshot.get("lines", [])]
        visible = "\n".join(line for line in lines if line)
        self._process_text(terminal, visible)

    def _process_text(self, terminal, text):
        tid = id(terminal)
        try:
            titlebar = terminal._titlebar
        except AttributeError:
            return

        # Check for build completion first
        if self._COMPLETE_PATTERN.search(text):
            self._tracking.pop(tid, None)
            titlebar.set_activity(False)
            titlebar._activity_label.setToolTip("Activity detected")
            return

        # Try fraction pattern first (more specific)
        percent = None
        match = self._FRACTION_PATTERN.search(text)
        if match:
            current, total = int(match.group(1)), int(match.group(2))
            if total > 0:
                percent = int(100 * current / total)

        # Fall back to percentage pattern
        if percent is None:
            match = self._PERCENT_PATTERN.search(text)
            if match:
                percent = int(match.group(1))

        if percent is not None:
            percent = max(0, min(100, percent))
            last = self._tracking.get(tid)
            # Only update if changed to avoid excessive repaints
            if last != percent:
                self._tracking[tid] = percent
                titlebar.set_activity(True)
                titlebar._activity_label.setStyleSheet(
                    "color: #3498db; font-size: 10px;"  # blue
                )
                titlebar._activity_label.setToolTip(f"Build progress: {percent}%")

            # Auto-clear on 100%
            if percent >= 100:
                self._tracking.pop(tid, None)
                titlebar.set_activity(False)
                titlebar._activity_label.setToolTip("Activity detected")


# ---------------------------------------------------------------------------
# 3. LongCommandNotifier
# ---------------------------------------------------------------------------

class LongCommandNotifier(_ShadowWatcher):
    """Sends a desktop notification when a command finishes after prolonged output."""

    name = "long_command_notifier"
    description = "Notifies when a long-running command finishes (30s silence)"
    version = "1.0"
    capabilities = ["output_watcher"]

    SILENCE_THRESHOLD = 30.0  # seconds
    # Minimum bytes of output before we consider it "significant"
    MIN_OUTPUT_BYTES = 500
    USE_RAW_OUTPUT = True

    def __init__(self):
        super().__init__()
        # terminal id -> {last_time, total_bytes, notified}
        self._state = {}

    def on_output(self, terminal, text):
        tid = id(terminal)
        now = time.monotonic()

        state = self._state.get(tid)
        if state is None:
            state = {
                "last_time": now,
                "total_bytes": 0,
                "notified": False,
            }
            self._state[tid] = state

        elapsed = now - state["last_time"]

        # If there was a long gap after significant output, the previous
        # command likely finished and a new one is starting.
        if (
            elapsed >= self.SILENCE_THRESHOLD
            and state["total_bytes"] >= self.MIN_OUTPUT_BYTES
            and not state["notified"]
        ):
            # Only notify if terminal is not focused
            try:
                is_focused = terminal.is_active()
            except AttributeError:
                is_focused = False

            if not is_focused:
                try:
                    subprocess.Popen(
                        ["notify-send", "QTerminator", "Command finished"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except FileNotFoundError:
                    pass  # notify-send not installed
            state["notified"] = True

        # If new output arrives after silence, reset tracking for the new command
        if elapsed >= self.SILENCE_THRESHOLD:
            state["total_bytes"] = 0
            state["notified"] = False

        state["last_time"] = now
        state["total_bytes"] += len(text)


# ---------------------------------------------------------------------------
# 4. LogLevelColorizer
# ---------------------------------------------------------------------------

class LogLevelColorizer(_ShadowWatcher):
    """Counts log levels in recent output and colors the activity indicator."""

    name = "log_level_colorizer"
    description = "Colors the activity dot based on log level distribution"
    version = "1.0"
    capabilities = ["output_watcher"]

    _LOG_PATTERN = re.compile(
        r'\b(DEBUG|INFO|WARN(?:ING)?|ERROR)\b', re.IGNORECASE
    )

    # Keep a rolling window of recent log levels
    WINDOW_SIZE = 200

    # Dot colors by dominant level
    _COLORS = {
        "error": "#e74c3c",   # red
        "warn": "#f39c12",    # yellow/orange
        "info": "#2ecc71",    # green
        "debug": "#95a5a6",   # grey
    }

    def __init__(self):
        super().__init__()
        # terminal id -> deque of normalised level strings
        self._history = {}

    def on_output(self, terminal, text):
        self._record_levels(terminal, text, append=True)

    def on_snapshot(self, terminal, snapshot):
        self._record_levels(terminal, _snapshot_text(snapshot), append=False)

    def _record_levels(self, terminal, text, append: bool):
        tid = id(terminal)
        if tid not in self._history:
            self._history[tid] = deque(maxlen=self.WINDOW_SIZE)

        history = self._history[tid]
        if not append:
            history.clear()

        for match in self._LOG_PATTERN.finditer(text):
            level = match.group(1).upper()
            if level.startswith("WARN"):
                level = "WARN"
            history.append(level)

        if not history:
            return

        # Count levels
        counts = {"DEBUG": 0, "INFO": 0, "WARN": 0, "ERROR": 0}
        for lvl in history:
            counts[lvl] = counts.get(lvl, 0) + 1

        total = len(history)

        # Determine dominant level (ERROR > WARN > INFO > DEBUG)
        if counts["ERROR"] > 0:
            dominant = "error"
        elif counts["WARN"] > 0:
            dominant = "warn"
        elif counts["INFO"] > 0:
            dominant = "info"
        else:
            dominant = "debug"

        color = self._COLORS[dominant]
        tooltip = (
            f"Log levels (last {total}): "
            f"ERROR={counts['ERROR']} WARN={counts['WARN']} "
            f"INFO={counts['INFO']} DEBUG={counts['DEBUG']}"
        )

        try:
            titlebar = terminal._titlebar
            titlebar.set_activity(True)
            titlebar._activity_label.setStyleSheet(
                f"color: {color}; font-size: 10px;"
            )
            titlebar._activity_label.setToolTip(tooltip)
        except AttributeError:
            pass


# ---------------------------------------------------------------------------
# 5. SensitiveDataWarner
# ---------------------------------------------------------------------------

class SensitiveDataWarner(_ShadowWatcher):
    """Warns when potential secrets appear in terminal output."""

    name = "sensitive_data_warner"
    description = "Flags AWS keys, API tokens, and private keys in output"
    version = "1.0"
    capabilities = ["output_watcher"]

    _PATTERNS = [
        # AWS Access Key IDs start with AKIA
        re.compile(r'AKIA[0-9A-Z]{16}'),
        # Private key headers
        re.compile(r'-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----'),
        # Long base64 strings after key/token/password/secret keywords
        re.compile(
            r'(?:token|key|password|secret|apikey|api_key|access_key)'
            r'\s*[=:]\s*["\']?'
            r'([A-Za-z0-9+/=_\-]{20,})',
            re.IGNORECASE,
        ),
    ]

    def __init__(self):
        super().__init__()
        self._warned = set()  # terminal ids already warned

    def on_output(self, terminal, text):
        self._process_text(terminal, text)

    def on_snapshot(self, terminal, snapshot):
        self._process_text(terminal, _snapshot_text(snapshot))

    def _process_text(self, terminal, text):
        tid = id(terminal)
        for pattern in self._PATTERNS:
            if pattern.search(text):
                try:
                    titlebar = terminal._titlebar
                    titlebar.set_activity(True)
                    titlebar._activity_label.setStyleSheet(
                        "color: #e74c3c; font-size: 10px;"  # red
                    )
                    titlebar._activity_label.setToolTip(
                        "\u26a0 Possible secret/credential detected in output!"
                    )
                    self._warned.add(tid)
                except AttributeError:
                    pass
                break  # one match is enough
