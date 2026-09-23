"""Background-worker plumbing so long file operations never freeze the UI.

QFileMan does several things that walk a tree or stream a whole file — a
recursive folder-size scan, a content search, a multi-gigabyte checksum.
Running any of those in the slot that handles the button click pins the Qt
event loop: the window stops repainting and stops answering input until the
operation finishes. This module gives those call sites a tiny, uniform way
to push the work onto a :class:`~PyQt6.QtCore.QThread` and stream results
back to the GUI thread over signals.

Two pieces:

* :class:`Worker` — a ``QObject`` that runs one callable on whatever thread
  it lives on, emitting :pyattr:`progress` / :pyattr:`result` /
  :pyattr:`error` / :pyattr:`finished`. The callable is handed a
  :class:`Cancellation` token plus a ``progress`` callback so it can report
  partial work and bail out early when the user cancels.
* :class:`Cancellation` — a thread-safe flag the GUI thread sets and the
  worker thread polls. It carries no Qt dependency, so the pure walk/hash
  helpers can accept it without importing PyQt.

:func:`run_in_thread` wires a ``Worker`` to a fresh ``QThread``, starts it,
and guarantees the thread is torn down (``quit`` + ``deleteLater``) once the
job ends — including on error or cancel — so repeated operations don't leak
threads.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QObject, QThread, pyqtSignal

log = logging.getLogger(__name__)


class Cancellation:
    """Thread-safe cooperative-cancel flag.

    Deliberately Qt-free: the pure helpers (``directory_size``,
    ``hash_file``, ``FileSearch``) take one of these and poll
    :meth:`is_set` / :meth:`raise_if_cancelled` in their inner loop, so the
    same code is testable without a running event loop.
    """

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        """Request cancellation (callable from any thread)."""
        self._event.set()

    def is_set(self) -> bool:
        """Return ``True`` once :meth:`cancel` has been called."""
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        """Raise :class:`Cancelled` if cancellation has been requested."""
        if self._event.is_set():
            raise Cancelled()


class Cancelled(Exception):
    """Raised inside a worker callable to unwind once cancelled."""


# A worker callable receives the cancel token and a ``progress(done, total,
# label)`` reporter, and returns whatever it wants delivered on ``result``.
WorkerFn = Callable[["Cancellation", Callable[..., None]], Any]


class Worker(QObject):
    """Runs a single callable, reporting back over Qt signals.

    Create it, move it onto a ``QThread`` (or use :func:`run_in_thread`),
    and connect the thread's ``started`` signal to :meth:`run`. The callable
    runs entirely on the worker thread; only the *signals* cross back to the
    GUI thread, where Qt's queued connections make them safe to touch
    widgets from.
    """

    #: ``(done, total, label)`` — ``total`` is ``-1`` when unknown.
    progress = pyqtSignal(int, int, str)
    #: The callable's return value (delivered only on success).
    result = pyqtSignal(object)
    #: ``str`` message when the callable raised (other than cancel).
    error = pyqtSignal(str)
    #: Always emitted exactly once, last, regardless of outcome.
    finished = pyqtSignal()

    def __init__(self, fn: WorkerFn, cancel: Cancellation | None = None) -> None:
        super().__init__()
        self._fn = fn
        self.cancel = cancel or Cancellation()

    def request_cancel(self) -> None:
        """Ask the running callable to stop at its next checkpoint."""
        self.cancel.cancel()

    def run(self) -> None:
        """Execute the callable; emit result/error then finished.

        ``finished`` is emitted in a ``finally`` so a crashing callable can
        never wedge the thread teardown in :func:`run_in_thread`.
        """
        try:
            value = self._fn(self.cancel, self._emit_progress)
        except Cancelled:
            log.debug("worker cancelled")
        except Exception as exc:  # noqa: BLE001 — surface, don't crash the UI
            log.warning("worker failed: %s", exc)
            self.error.emit(str(exc))
        else:
            # Suppress a result that arrived after a late cancel so the UI
            # doesn't render output the user asked to abandon.
            if not self.cancel.is_set():
                self.result.emit(value)
        finally:
            self.finished.emit()

    def _emit_progress(self, done: int, total: int = -1, label: str = "") -> None:
        self.progress.emit(int(done), int(total), str(label))


# Started threads (and their workers) are pinned here for the duration of the
# run. Without this, a caller that drops the returned thread lets Python
# garbage-collect a QThread mid-run, tripping "QThread: Destroyed while thread
# is still running" and aborting the process. Entries are removed once the
# thread has stopped.
_LIVE: set[QThread] = set()


def run_in_thread(worker: Worker) -> QThread:
    """Start ``worker`` on a fresh ``QThread`` and return that thread.

    The thread is wired to quit when the worker finishes and to delete both
    itself and the worker afterwards, so callers don't have to manage the
    lifecycle. The helper also pins the thread (and, via a Python attribute,
    its worker) in a module-level registry for the duration of the run, so it
    stays alive even if the caller discards the returned thread.
    """
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    # Delete the worker only *after the thread has actually stopped*
    # (thread.finished) — deleting a QObject that still lives on a running
    # thread, or a QThread mid-run, aborts the process. The thread deletes
    # itself the same way. Keep the worker referenced from the thread so the
    # registry entry below keeps both alive.
    thread._qfileman_worker = worker  # type: ignore[attr-defined]
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    _LIVE.add(thread)
    thread.finished.connect(lambda t=thread: _LIVE.discard(t))
    thread.start()
    return thread


class ProgressRunner(QObject):
    """Drive a :class:`Worker` behind a cancellable ``QProgressDialog``.

    A menu callback that used to run a long operation inline can hand it here
    instead: the work runs on a background thread, a modeless progress dialog
    shows live progress with a *Cancel* button wired to the worker's cancel
    token, and ``on_result`` fires on the GUI thread when (and only if) the
    work completed without being cancelled. ``on_error`` fires on failure.

    The runner keeps the worker, thread and progress dialog alive for its own
    lifetime, so the caller only has to keep the ``ProgressRunner`` reference
    until it's done (typically by stashing it on a parent widget).
    """

    def __init__(
        self,
        fn: WorkerFn,
        *,
        title: str,
        label: str,
        parent=None,
        on_result: Callable[[Any], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(parent)
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QProgressDialog

        self._on_result = on_result
        self._on_error = on_error

        self._worker = Worker(fn)
        self._worker.progress.connect(self._on_progress)
        self._worker.result.connect(self._on_worker_result)
        self._worker.error.connect(self._on_worker_error)

        self._dialog = QProgressDialog(label, "Cancel", 0, 0, parent)
        self._dialog.setWindowTitle(title)
        self._dialog.setWindowModality(Qt.WindowModality.WindowModal)
        # Don't auto-pop for sub-second work; don't auto-close on its own —
        # we drive close/reset explicitly from the worker lifecycle.
        self._dialog.setMinimumDuration(300)
        self._dialog.setAutoClose(False)
        self._dialog.setAutoReset(False)
        self._dialog.canceled.connect(self._worker.request_cancel)

    def start(self):
        """Begin the run; returns the backing ``QThread``."""
        self._thread = run_in_thread(self._worker)
        # Close the progress dialog once the worker thread is gone, and drop
        # the runner's self-reference so it can be collected.
        self._thread.finished.connect(self._finish)
        return self._thread

    def _on_progress(self, done: int, total: int, label: str) -> None:
        if total > 0:
            self._dialog.setMaximum(total)
            self._dialog.setValue(min(done, total))
        if label:
            self._dialog.setLabelText(label)

    def _on_worker_result(self, value) -> None:
        if self._on_result is not None:
            self._on_result(value)

    def _on_worker_error(self, message: str) -> None:
        if self._on_error is not None:
            self._on_error(message)

    def _finish(self) -> None:
        self._dialog.reset()
        self._dialog.deleteLater()
        self.deleteLater()
