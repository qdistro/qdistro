"""Non-blocking search dialog that wraps :class:`qfileman.search.FileSearch`.

The dialog exposes glob-pattern search and an optional substring content
filter. Double-clicking a result emits :pyattr:`path_chosen` so the main
window can navigate to the result's parent directory and select it.

The actual tree walk runs on a :class:`~qfileman.worker` ``QThread`` and
streams matches back to the GUI thread in small batches, so a content search
over a deep tree never freezes the window. A *Search* press while a walk is
already running cancels the previous one before starting the new query, and
the button doubles as a *Cancel* control mid-search.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from qfileman.search import FileSearch
from qfileman.worker import Cancellation

log = logging.getLogger(__name__)

# Hard cap on result rows so a pathological query doesn't hang the UI.
_MAX_RESULTS = 500
# Stream results to the GUI in batches this size to keep signal traffic low.
_BATCH = 50


class _SearchWorker(QObject):
    """Runs one :class:`FileSearch` query and streams matches in batches.

    Lives on a worker ``QThread``. Each emitted batch is a list of
    ``(display_text, path)`` tuples — plain data, never widgets, since
    ``QListWidgetItem`` must be created on the GUI thread.
    """

    batch = pyqtSignal(list)
    tagged_batch = pyqtSignal(int, list)
    #: ``(count, truncated)`` once the walk ends (success or cancel).
    done = pyqtSignal(int, bool)
    tagged_done = pyqtSignal(int, int, bool)
    finished = pyqtSignal()

    def __init__(
        self,
        root: Path,
        pattern: str,
        content: str,
        hidden: bool,
        case_sensitive: bool,
        cancel: Cancellation,
        generation: int | None = None,
    ) -> None:
        super().__init__()
        self._root = root
        self._pattern = pattern
        self._content = content
        self._hidden = hidden
        self._case_sensitive = case_sensitive
        self.cancel = cancel
        self._generation = generation

    def run(self) -> None:
        try:
            self._search()
        except Exception as exc:  # noqa: BLE001 — never crash the UI thread
            log.warning("search worker failed: %s", exc)
            self._emit_done(0, False)
        finally:
            self.finished.emit()

    def _search(self) -> None:
        searcher = FileSearch(self._root)
        pending: list[tuple[str, str]] = []
        count = 0
        truncated = False

        def flush() -> None:
            nonlocal pending
            if pending:
                if self._generation is None:
                    self.batch.emit(pending)
                else:
                    self.tagged_batch.emit(self._generation, pending)
                pending = []

        if self._content:
            rows = searcher.by_content(
                self._content,
                pattern=self._pattern,
                hidden=self._hidden,
                case_sensitive=self._case_sensitive,
                is_cancelled=self.cancel.is_set,
            )
            for fp, line_no, line_text in rows:
                pending.append(
                    (f"{fp}  :{line_no}:  {line_text.strip()}", str(fp))
                )
                count += 1
                if len(pending) >= _BATCH:
                    flush()
                if count >= _MAX_RESULTS:
                    truncated = True
                    break
        else:
            for fp in searcher.by_name(
                self._pattern,
                hidden=self._hidden,
                is_cancelled=self.cancel.is_set,
            ):
                pending.append((str(fp), str(fp)))
                count += 1
                if len(pending) >= _BATCH:
                    flush()
                if count >= _MAX_RESULTS:
                    truncated = True
                    break

        flush()
        self._emit_done(count, truncated)

    def _emit_done(self, count: int, truncated: bool) -> None:
        if self._generation is None:
            self.done.emit(count, truncated)
        else:
            self.tagged_done.emit(self._generation, count, truncated)


class _ThreadFinishRelay(QObject):
    """Deliver one thread stop only while its SearchDialog still exists.

    The relay is a native child of the dialog, so Qt destroys it and removes
    its queued connections with the dialog.  Unlike an anonymous Python
    closure, this bound QObject slot therefore cannot run later against a
    deleted dialog wrapper.
    """

    def __init__(self, dialog: SearchDialog, generation: int) -> None:
        super().__init__(dialog)
        self._generation = generation

    @pyqtSlot()
    def deliver(self) -> None:
        dialog = self.parent()
        if dialog is None:
            return
        dialog._on_thread_finished(self._generation)
        self.deleteLater()


class SearchDialog(QDialog):
    """Modal-but-non-blocking search dialog rooted at a given directory."""

    path_chosen = pyqtSignal(str)

    def __init__(self, root: str | Path, parent=None, show_hidden: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle("Find")
        self.resize(560, 420)
        self._root = Path(root)
        self._show_hidden = show_hidden
        self._thread = None
        self._worker = None
        self._cancel: Cancellation | None = None
        self._busy = False
        self._count = 0
        self._generation = 0
        self._done_received = False
        self._thread_stopped = False
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.pattern_edit = QLineEdit("*")
        self.pattern_edit.setToolTip("Glob pattern, e.g. *.py")
        form.addRow("Filename pattern:", self.pattern_edit)

        self.content_edit = QLineEdit()
        self.content_edit.setPlaceholderText("(optional) only files containing this text")
        form.addRow("Content contains:", self.content_edit)

        self.cb_case = QCheckBox("Case sensitive")
        self.cb_hidden = QCheckBox("Include hidden files")
        self.cb_hidden.setChecked(self._show_hidden)
        opts = QHBoxLayout()
        opts.addWidget(self.cb_case)
        opts.addWidget(self.cb_hidden)
        opts.addStretch(1)
        form.addRow("", self._wrap(opts))
        layout.addLayout(form)

        self.root_label = QLabel(f"Searching in: {self._root}")
        self.root_label.setWordWrap(True)
        layout.addWidget(self.root_label)

        controls = QHBoxLayout()
        self.search_btn = QPushButton("Search")
        self.search_btn.setDefault(True)
        self.search_btn.clicked.connect(self._on_search_clicked)
        controls.addWidget(self.search_btn)
        controls.addStretch(1)
        self.status_label = QLabel("")
        controls.addWidget(self.status_label)
        layout.addLayout(controls)

        self.results = QListWidget()
        self.results.itemActivated.connect(self._on_result_activated)
        layout.addWidget(self.results, 1)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=self)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    @staticmethod
    def _wrap(inner_layout):
        from PyQt6.QtWidgets import QWidget

        w = QWidget()
        w.setLayout(inner_layout)
        return w

    # ------------------------------------------------------------------ search
    @property
    def is_searching(self) -> bool:
        """``True`` while a worker walk is in flight."""
        return self._busy

    def _on_search_clicked(self) -> None:
        """Start a search, or cancel the one already running."""
        if self.is_searching:
            self._request_cancel()
            return
        self._run_search()

    def _run_search(self) -> None:
        """Kick off the configured search on a worker thread.

        Returns immediately; generation-tagged results stream into the GUI
        and the run is finalised after both its terminal result and thread
        exit arrive. Use :meth:`wait_for_search`
        (tests) to block until the walk completes.
        """
        # A fresh press supersedes any in-flight walk: cancel it and block
        # until its thread has actually stopped, so we never run two walks at
        # once. The old worker schedules its deletion before its event loop
        # exits and the old thread deletes itself after stopping.
        if self.is_searching:
            self._request_cancel()
            self.wait_for_search()

        self.results.clear()
        self._count = 0
        pattern = self.pattern_edit.text().strip() or "*"
        content = self.content_edit.text()
        hidden = self.cb_hidden.isChecked()
        case_sensitive = self.cb_case.isChecked()

        self._cancel = Cancellation()
        self._generation += 1
        generation = self._generation
        self._done_received = False
        self._thread_stopped = False
        worker = _SearchWorker(
            self._root,
            pattern,
            content,
            hidden,
            case_sensitive,
            self._cancel,
            generation,
        )
        # Never call QObject.sender() for queued signals from a short-lived
        # worker.  The native sender may already have been deleted by the time
        # the GUI dequeues the signal, making sender() a use-after-free hazard
        # in PyQt.  A monotonic run token rejects stale signals without
        # dereferencing their sender.
        worker.tagged_batch.connect(self._accept_batch)
        worker.tagged_done.connect(self._accept_done)
        thread = run_in_thread_for(worker)
        # Flip the busy flag off only after the tagged terminal outcome and
        # the native thread stop have both reached the GUI. The QObject relay
        # is parented to this dialog, so deleting the dialog automatically
        # disconnects a finish that is still queued.
        finish_relay = _ThreadFinishRelay(self, generation)
        thread._qfileman_finish_relay = finish_relay  # type: ignore[attr-defined]
        thread.finished.connect(finish_relay.deliver)
        self._worker = worker
        self._thread = thread
        self._busy = True

        self.status_label.setText("Searching…")
        self.search_btn.setText("Cancel")

    def _on_batch(self, rows: list) -> None:
        """Compatibility slot used by direct-signal tests.

        Production connections use :meth:`_accept_batch` and never inspect a
        potentially deleted queued-signal sender.
        """
        # Identity-gate: accept rows only from the *current* worker. A
        # superseded worker that outlived wait_for_search()'s timeout could
        # still have queued batches in flight; checking the emitter (not just
        # _busy) keeps its stale results out of the new search's list.
        # QThread.finished and the worker's queued batch can reach the GUI
        # event queue in either order. Identity is the stale-run boundary;
        # `_busy` is deliberately not one, because the current worker may
        # have stopped just before its final queued rows are delivered.
        if self.sender() is not self._worker:
            return
        self._append_batch(rows)

    def _accept_batch(self, generation: int, rows: list) -> None:
        if generation != self._generation:
            return
        self._append_batch(rows)

    def _append_batch(self, rows: list) -> None:
        for display, path in rows:
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.results.addItem(item)
        self._count += len(rows)

    def _on_done(self, count: int, truncated: bool) -> None:
        """Compatibility slot used by direct-signal tests."""
        # Same identity-gate as _on_batch: a stale worker's terminal status
        # must not overwrite the current search's count.
        # As with batches, a valid queued done signal may be delivered just
        # after QThread.finished cleared `_busy`. The worker identity remains
        # current until another search starts, so it is the sufficient gate.
        if self.sender() is not self._worker:
            return
        self._record_done(count, truncated)

    def _accept_done(self, generation: int, count: int, truncated: bool) -> None:
        if generation != self._generation:
            return
        self._record_done(count, truncated)
        self._done_received = True
        self._finish_if_complete()

    def _record_done(self, count: int, truncated: bool) -> None:
        self._count = count
        suffix = " (truncated)" if truncated else ""
        plural = "es" if count != 1 else ""
        self.status_label.setText(f"{count} match{plural}{suffix}")

    def _request_cancel(self) -> None:
        if self._cancel is not None:
            self._cancel.cancel()
        self.status_label.setText("Cancelled")

    def _on_thread_finished(self, generation: int) -> None:
        """Finalise once the worker thread has fully stopped.

        Only the current generation contributes to completion; a late finish
        from a superseded run is ignored. The busy flag remains set until its
        queued terminal outcome has also been delivered.
        """
        if generation != self._generation:
            return
        self._thread_stopped = True
        self._finish_if_complete()

    def _finish_if_complete(self) -> None:
        """Publish idle only after result delivery and native thread exit."""
        if not (self._done_received and self._thread_stopped):
            return
        self._busy = False
        self.search_btn.setText("Search")

    def wait_for_search(self, timeout_ms: int = 5000) -> bool:
        """Block (pumping the event loop) until the current search ends.

        Test/synchronous helper. Returns ``True`` if the walk finished within
        ``timeout_ms``. Real GUI use never calls this — results arrive via the
        streamed signals.
        """
        from PyQt6.QtCore import QDeadlineTimer, QEventLoop
        from PyQt6.QtWidgets import QApplication

        deadline = QDeadlineTimer(timeout_ms)
        app = QApplication.instance()
        while self.is_searching and not deadline.hasExpired():
            if app is not None:
                app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        return not self.is_searching

    def reject(self) -> None:  # noqa: D401 — Qt override
        """Cancel any in-flight walk before closing."""
        if self.is_searching:
            self._request_cancel()
        super().reject()

    def _on_result_activated(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if path:
            self.path_chosen.emit(path)


def run_in_thread_for(worker: _SearchWorker):
    """Start a :class:`_SearchWorker` on a fresh thread.

    For this streaming search worker, schedule deletion in its affinity
    thread when the worker finishes, delete the thread wrapper after the
    native thread stops, and pin both until shutdown completes.
    """
    from PyQt6.QtCore import QThread

    from qfileman.worker import _LIVE

    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    # Schedule this search worker's deferred deletion from its own terminal
    # signal, while it still has worker-thread affinity. Generation-tagged GUI
    # signals make any independently queued final result safe after deletion.
    worker.finished.connect(worker.deleteLater)
    thread._qfileman_worker = worker  # type: ignore[attr-defined]
    thread.finished.connect(thread.deleteLater)
    _LIVE.add(thread)
    thread.finished.connect(lambda t=thread: _LIVE.discard(t))
    thread.start()
    return thread
