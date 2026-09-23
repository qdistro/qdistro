"""Tests for SearchDialog."""

from __future__ import annotations

import threading

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent, Qt

# isort: split
import qfileman.search_dialog as search_dialog_mod
from qfileman.search_dialog import SearchDialog, _SearchWorker
from qfileman.worker import _LIVE


def _result_paths(dlg):
    return [
        dlg.results.item(i).data(Qt.ItemDataRole.UserRole)
        for i in range(dlg.results.count())
    ]


def test_search_dialog_name_pattern(qapp, tmp_tree):
    dlg = SearchDialog(tmp_tree)
    try:
        dlg.pattern_edit.setText("*.py")
        dlg._run_search()
        assert dlg.wait_for_search()
        paths = _result_paths(dlg)
        assert len(paths) == 1
        assert paths[0].endswith("file2.py")
        assert "1 match" in dlg.status_label.text()
    finally:
        dlg.deleteLater()


def test_search_dialog_content_filter(qapp, tmp_tree):
    dlg = SearchDialog(tmp_tree)
    try:
        dlg.content_edit.setText("hello")
        dlg._run_search()
        assert dlg.wait_for_search()
        paths = _result_paths(dlg)
        assert len(paths) == 1
        assert paths[0].endswith("file1.txt")
    finally:
        dlg.deleteLater()


def test_search_dialog_hidden_toggle(qapp, tmp_tree):
    dlg = SearchDialog(tmp_tree, show_hidden=False)
    try:
        dlg.pattern_edit.setText("*")
        dlg._run_search()
        assert dlg.wait_for_search()
        paths_visible = _result_paths(dlg)
        assert not any(p.endswith(".hidden") for p in paths_visible)

        dlg.cb_hidden.setChecked(True)
        dlg._run_search()
        assert dlg.wait_for_search()
        paths_with_hidden = _result_paths(dlg)
        assert any(p.endswith(".hidden") for p in paths_with_hidden)
    finally:
        dlg.deleteLater()


def test_search_dialog_path_chosen_signal(qapp, tmp_tree):
    received = []
    dlg = SearchDialog(tmp_tree)
    try:
        dlg.path_chosen.connect(received.append)
        dlg.pattern_edit.setText("file1.txt")
        dlg._run_search()
        assert dlg.wait_for_search()
        assert dlg.results.count() == 1
        dlg._on_result_activated(dlg.results.item(0))
        assert len(received) == 1
        assert received[0].endswith("file1.txt")
    finally:
        dlg.deleteLater()


def test_search_dialog_empty_pattern_defaults_to_star(qapp, tmp_tree):
    dlg = SearchDialog(tmp_tree)
    try:
        dlg.pattern_edit.setText("")  # whitespace fallback
        dlg._run_search()
        assert dlg.wait_for_search()
        # Should still produce results (all visible files)
        assert dlg.results.count() > 0
    finally:
        dlg.deleteLater()


def test_search_dialog_zero_matches_status(qapp, tmp_tree):
    dlg = SearchDialog(tmp_tree)
    try:
        dlg.pattern_edit.setText("*.no_such_ext")
        dlg._run_search()
        assert dlg.wait_for_search()
        assert dlg.results.count() == 0
        assert "0 match" in dlg.status_label.text()
    finally:
        dlg.deleteLater()


# --------------------------------------------------------------------------
# Threading behaviour: the walk must not block the GUI thread, must be
# cancellable, and a fresh search must supersede an in-flight one.
# --------------------------------------------------------------------------
def test_search_runs_on_worker_thread(qapp, tmp_tree):
    """_run_search returns immediately with the walk still in flight."""
    dlg = SearchDialog(tmp_tree)
    try:
        dlg.pattern_edit.setText("*")
        dlg._run_search()
        # Returned without blocking: a search is in progress.
        assert dlg.is_searching is True
        assert dlg.search_btn.text() == "Cancel"
        assert dlg.wait_for_search()
        assert dlg.is_searching is False
        assert dlg.search_btn.text() == "Search"
    finally:
        dlg.deleteLater()


def test_search_cancel_stops_and_resets(qapp, tmp_tree):
    dlg = SearchDialog(tmp_tree)
    try:
        dlg.pattern_edit.setText("*")
        dlg._run_search()
        # Status flips to "Cancelled" immediately on the cancel request; the
        # walk itself may finish first on this tiny tree, but either way the
        # dialog must end not-searching with the button reset.
        dlg._request_cancel()
        assert "Cancelled" in dlg.status_label.text()
        assert dlg.wait_for_search()
        assert dlg.is_searching is False
        assert dlg.search_btn.text() == "Search"
    finally:
        dlg.deleteLater()


def test_second_search_supersedes_first(qapp, tmp_tree):
    """A fresh _run_search cancels the prior run and reports clean results."""
    dlg = SearchDialog(tmp_tree)
    try:
        dlg.pattern_edit.setText("*")
        dlg._run_search()
        # Immediately start a narrower search; the first is superseded.
        dlg.pattern_edit.setText("*.py")
        dlg._run_search()
        assert dlg.wait_for_search()
        paths = _result_paths(dlg)
        assert len(paths) == 1
        assert paths[0].endswith("file2.py")
        assert "1 match" in dlg.status_label.text()
    finally:
        dlg.deleteLater()


def test_search_button_click_toggles_cancel(qapp, tmp_tree):
    dlg = SearchDialog(tmp_tree)
    try:
        dlg.pattern_edit.setText("*")
        dlg._on_search_clicked()  # start
        assert dlg.is_searching is True
        dlg._on_search_clicked()  # second click cancels
        assert dlg.wait_for_search()
        assert dlg.is_searching is False
    finally:
        dlg.deleteLater()


def test_stale_worker_signals_are_ignored(qapp, tmp_tree):
    """A superseded worker's late batch/done must not pollute the new run.

    Simulates the race codex flagged: the previous worker's thread outlived
    wait_for_search()'s timeout and its queued signals arrive *after* a new
    search has started. The identity gate (sender() is not self._worker)
    must drop them.
    """
    dlg = SearchDialog(tmp_tree)
    try:
        # Start and settle a search so there is a current worker.
        dlg.pattern_edit.setText("*.py")
        dlg._run_search()
        assert dlg.wait_for_search()
        baseline = dlg.results.count()
        assert "1 match" in dlg.status_label.text()

        # Fabricate a stale worker that is NOT the dialog's current worker and
        # fire its result handlers directly with bogus data.
        from qfileman.search_dialog import _SearchWorker
        from qfileman.worker import Cancellation

        stale = _SearchWorker(
            tmp_tree, "*", "", False, False, Cancellation()
        )
        stale.batch.connect(dlg._on_batch)
        stale.done.connect(dlg._on_done)
        # Pretend a fresh search is in flight so the _busy guard alone would
        # have accepted these; only the identity gate should reject them.
        dlg._busy = True
        stale.batch.emit([("BOGUS  ", "/bogus/path")])
        stale.done.emit(999, True)
        qapp.processEvents()

        assert dlg.results.count() == baseline, "stale batch leaked into results"
        assert "999" not in dlg.status_label.text(), "stale done overwrote status"
    finally:
        dlg._busy = False
        dlg.deleteLater()


def test_search_completion_waits_for_done_and_thread_stop(qapp, tmp_tree):
    """The synchronous completion boundary includes both queued outcomes.

    A worker can stop before its queued ``done`` reaches the GUI thread (or
    vice versa).  Reporting idle at either half-boundary lets a caller delete
    the dialog while a queued callback still targets it.
    """
    dlg = SearchDialog(tmp_tree)
    try:
        dlg._busy = True
        dlg._generation = 7

        dlg._on_thread_finished(7)
        assert dlg.is_searching is True, (
            "thread exit alone exposed a half-delivered search as complete"
        )

        dlg._accept_done(7, 3, False)
        assert dlg.is_searching is False
        assert dlg.status_label.text() == "3 matches"
    finally:
        dlg.deleteLater()


def test_search_generation_gate_rejects_stale_queued_data(qapp, tmp_tree):
    """A deleted/superseded sender is rejected by token, not sender()."""
    dlg = SearchDialog(tmp_tree)
    try:
        dlg._generation = 2
        dlg._accept_batch(1, [("BOGUS", "/bogus")])
        dlg._accept_done(1, 999, True)
        assert dlg.results.count() == 0
        assert "999" not in dlg.status_label.text()
    finally:
        dlg.deleteLater()


def test_repeated_search_workers_deliver_then_destroy(qapp, tmp_tree):
    """Real worker threads fully deliver and drain before completion.

    Repeating the native QThread lifecycle makes the queued ``done`` versus
    ``finished`` ordering race observable without calling private result
    slots directly.  Every iteration must deliver its result, delete both
    native Qt objects, and leave the shared live-thread registry empty.
    """
    for iteration in range(100):
        dlg = SearchDialog(tmp_tree)
        try:
            dlg.pattern_edit.setText("*.py")
            dlg._run_search()
            worker = dlg._worker
            thread = dlg._thread

            assert dlg.wait_for_search(), f"search {iteration} wedged"
            assert dlg.results.count() == 1, \
                f"search {iteration} completed before result delivery"
            assert dlg.status_label.text() == "1 match"
            assert not _LIVE, f"search {iteration} left a live QThread"
            assert sip.isdeleted(worker), \
                f"search {iteration} leaked its native worker"
            assert sip.isdeleted(thread), \
                f"search {iteration} leaked its native thread"
        finally:
            dlg.deleteLater()
            qapp.processEvents()


def test_dialog_deleted_between_done_and_thread_stop_is_safe(
    qapp, qtbot, tmp_tree, monkeypatch
):
    """Destroying the dialog cannot leave a queued finish targeting it."""
    release_thread = threading.Event()

    class PausingSearchWorker(_SearchWorker):
        def _search(self) -> None:
            # Deliver the terminal outcome, then keep the real native thread
            # alive so the test can destroy its receiver before ``finished``.
            self._emit_done(0, False)
            assert release_thread.wait(5), "test did not release worker thread"

    monkeypatch.setattr(search_dialog_mod, "_SearchWorker", PausingSearchWorker)
    dlg = SearchDialog(tmp_tree)
    dlg.pattern_edit.setText("*.no_such_ext")
    dlg._run_search()
    worker = dlg._worker
    thread = dlg._thread
    finish_relay = thread._qfileman_finish_relay

    try:
        qtbot.waitUntil(lambda: dlg._done_received, timeout=5000)
        assert thread.isRunning(), "thread stopped before destruction window"

        dlg.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert sip.isdeleted(dlg), "dialog was not natively destroyed"
        assert sip.isdeleted(finish_relay), \
            "dialog destruction left its finish receiver alive"

        release_thread.set()
        qtbot.waitUntil(lambda: not _LIVE, timeout=5000)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert sip.isdeleted(worker), "native worker survived thread shutdown"
        assert sip.isdeleted(thread), "native thread wrapper survived shutdown"
    finally:
        release_thread.set()
