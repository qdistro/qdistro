"""Shared helpers for plugins that spawn long-running external commands.

The leading underscore keeps this file out of plugin discovery (see
``PluginManager.discover`` which skips ``_*.py``) while still allowing
sibling plugins to import it as ``qfileman.plugins.builtin._runner``.

Beyond running the process, this module is also where the qdshell
notification bridge lives: every :class:`CommandDialog` posts a
*Started* notification at launch and replaces it with a *Done* or
*Failed* notification when the process exits. If the output happens to
include an rsync ``--info=progress2`` line we extract the percentage
and surface it on the same notification, throttled to once a second so
we don't hammer the bus.
"""

from __future__ import annotations

import logging
import re
import shutil
import time
from collections.abc import Iterable, Sequence

from PyQt6.QtCore import QProcess
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
)

log = logging.getLogger(__name__)


# rsync --info=progress2 emits lines like:
#       1,234,567   42%   10.5MB/s    0:00:12 (xfr#1, to-chk=2/3)
# We just want the percentage; the rest is noise.
_RSYNC_PROGRESS_RE = re.compile(r"\b(\d{1,3})%")


def parse_progress(text: str) -> int | None:
    """Return the last percentage in ``text``, or ``None`` if there isn't one.

    Picks the last match so that bursts of progress2 updates collapse to
    the most recent value. Clamps to 0..100.
    """
    matches = _RSYNC_PROGRESS_RE.findall(text)
    if not matches:
        return None
    try:
        value = int(matches[-1])
    except ValueError:
        return None
    return max(0, min(100, value))


def which(program: str) -> str | None:
    """Return the absolute path to ``program`` or ``None`` if not on PATH."""
    return shutil.which(program)


def missing_tools(programs: Iterable[str]) -> list[str]:
    """Return the subset of ``programs`` that are not on PATH."""
    return [p for p in programs if not which(p)]


class CommandDialog(QDialog):
    """Modal dialog that runs an argv command and streams its output.

    Output (both stdout and stderr) is appended to a read-only text view.
    A Cancel button is shown while the process runs and turns into Close
    once it exits. Closing the dialog mid-run kills the process.

    The dialog does not raise on a non-zero exit — the user can see the
    output and decide what to do. The exit code is exposed via
    :attr:`exit_code` after the process finishes.

    If ``notify`` is true (the default) a qdshell / Freedesktop
    notification is posted when the job starts and again when it
    finishes — replacing the original so the history stays clean.
    """

    def __init__(self, title: str, argv: Sequence[str], cwd: str | None = None,
                 *, notify: bool = True, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(700, 400)
        self.exit_code: int | None = None
        self._notify_enabled = notify
        self._notify_id: int = 0
        self._notify_title = title
        self._last_progress: int | None = None
        self._last_notify_ts: float = 0.0

        layout = QVBoxLayout(self)
        self._header = QLabel(self._format_argv(argv), self)
        self._header.setWordWrap(True)
        self._header.setTextInteractionFlags(
            self._header.textInteractionFlags()
            | self._header.textInteractionFlags().__class__.TextSelectableByMouse
        )
        layout.addWidget(self._header)

        self._output = QPlainTextEdit(self)
        self._output.setReadOnly(True)
        layout.addWidget(self._output, 1)

        self._buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, self)
        self._buttons.rejected.connect(self._on_cancel)
        layout.addWidget(self._buttons)

        self._process = QProcess(self)
        self._process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        if cwd:
            self._process.setWorkingDirectory(cwd)
        self._process.readyReadStandardOutput.connect(self._on_output)
        self._process.finished.connect(self._on_finished)
        self._process.errorOccurred.connect(self._on_error)

        program = argv[0]
        args = list(argv[1:])
        self._process.start(program, args)
        self._post_started()

    # ------------------------------------------------------------- notifications
    def _post_started(self) -> None:
        if not self._notify_enabled:
            return
        try:
            from qfileman.plugins.builtin import _qdshell
            summary, body = _qdshell.format_started(self._notify_title)
            self._notify_id = _qdshell.notify(summary, body, replaces_id=0)
        except Exception as e:
            # Notifications are best-effort. Never let a bus hiccup
            # interfere with the actual file operation.
            log.debug("qdshell start notification failed: %s", e)

    def _post_progress(self, value: int) -> None:
        if not self._notify_enabled:
            return
        now = time.monotonic()
        if value == self._last_progress and now - self._last_notify_ts < 1.0:
            return
        self._last_progress = value
        self._last_notify_ts = now
        try:
            from qfileman.plugins.builtin import _qdshell
            self._notify_id = _qdshell.notify(
                self._notify_title,
                f"{value}%",
                replaces_id=self._notify_id,
                value=value,
            )
        except Exception as e:
            log.debug("qdshell progress notification failed: %s", e)

    def _post_finished(self) -> None:
        if not self._notify_enabled:
            return
        try:
            from qfileman.plugins.builtin import _qdshell
            summary, body, urgency = _qdshell.format_finished(
                self._notify_title, self.exit_code
            )
            self._notify_id = _qdshell.notify(
                summary, body,
                replaces_id=self._notify_id,
                urgency=urgency,
            )
        except Exception as e:
            log.debug("qdshell finish notification failed: %s", e)

    # ----------------------------------------------------------------- ui
    @staticmethod
    def _format_argv(argv: Sequence[str]) -> str:
        # Best-effort shell-style preview; we never feed this back to a shell.
        return " ".join(argv)

    def _append(self, text: str) -> None:
        cursor = self._output.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text)
        self._output.setTextCursor(cursor)

    def _on_output(self) -> None:
        data = bytes(self._process.readAllStandardOutput()).decode(
            "utf-8", errors="replace"
        )
        self._append(data)
        value = parse_progress(data)
        if value is not None:
            self._post_progress(value)

    def _on_finished(self, exit_code: int, _exit_status) -> None:
        self.exit_code = exit_code
        self._append(f"\n--- exit code {exit_code} ---\n")
        self._buttons.setStandardButtons(QDialogButtonBox.StandardButton.Close)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.disconnect()
        self._buttons.rejected.connect(self.accept)
        self._post_finished()

    def _on_error(self, err) -> None:
        log.warning("QProcess error: %s", err)
        self._append(f"\n[process error: {err}]\n")

    def _on_cancel(self) -> None:
        if self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.kill()
            self._process.waitForFinished(2000)
        # Finish notification will be posted by _on_finished if the
        # process actually reaches that handler; if not (rare on kill),
        # do it now so the user sees the cancellation in qdshell.
        if self.exit_code is None:
            self._post_finished()
        self.reject()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        if self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.kill()
            self._process.waitForFinished(2000)
        super().closeEvent(event)


def run_command_dialog(title: str, argv: Sequence[str], cwd: str | None = None,
                       *, notify: bool = True, parent=None) -> int | None:
    """Run ``argv`` modally in a :class:`CommandDialog`. Returns exit code or None."""
    dlg = CommandDialog(title, argv, cwd=cwd, notify=notify, parent=parent)
    dlg.exec()
    return dlg.exit_code
