"""Unit tests for LockController.

The controller is pure state — auth is mocked. Verifies the property
notifications and the show-info/show-failure mutex match the
qdshell LockContext.qml behaviour.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QCoreApplication
from PyQt6.QtGui import QGuiApplication
from qdlocker.auth import AuthOutcome
from qdlocker.controller import LockController


@pytest.fixture(scope="session")
def qapp():
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    yield app


@pytest.fixture
def auth():
    """Auth backend stub with the same Signal surface as the real one."""
    from PyQt6.QtCore import QObject, pyqtSignal

    class StubAuth(QObject):
        ready = pyqtSignal()
        message = pyqtSignal(str, bool, bool)
        outcome = pyqtSignal(object)

        def __init__(self):
            super().__init__()
            self._gen = 0
            self.probe_pam = MagicMock(side_effect=lambda: self.ready.emit())
            self.start_pam = MagicMock()
            self.abort_pam = MagicMock()
            self.respond_pam = MagicMock()
            self.occupy_fingerprint_sensor = MagicMock()

        def reset_session(self):
            self._gen += 1

        def _current_generation(self):
            return self._gen

    return StubAuth()


def test_pam_ready_emitted_on_construction(qapp, auth):
    ctrl = LockController(auth)
    auth.probe_pam.assert_called_once()
    # ready signal is queued — pump the loop so _on_auth_ready fires
    QCoreApplication.processEvents()
    assert ctrl.pamReady is True


def test_typing_clears_info_and_failure(qapp, auth):
    ctrl = LockController(auth)
    ctrl._show_info = True
    ctrl._show_failure = True
    ctrl.currentText = "abc"
    assert ctrl.showInfo is False
    assert ctrl.showFailure is False
    auth.occupy_fingerprint_sensor.assert_called_with(True)


def test_show_info_and_failure_are_mutex(qapp, auth):
    ctrl = LockController(auth)
    ctrl._set_show_info(True)
    ctrl._set_show_failure(True)
    # showFailure-on flips showInfo off (matches LockContext.qml:66-76).
    assert ctrl.showInfo is False
    assert ctrl.showFailure is True


def test_successful_outcome_emits_unlocked(qapp, auth):
    ctrl = LockController(auth)
    sink = []
    ctrl.unlocked.connect(lambda: sink.append(True))
    auth.outcome.emit((AuthOutcome.SUCCESS, auth._current_generation()))
    QCoreApplication.processEvents()
    assert sink == [True]


def test_failed_outcome_clears_text_and_emits_failed(qapp, auth):
    ctrl = LockController(auth)
    ctrl._current_text = "wrongpw"
    sink = []
    ctrl.failed.connect(lambda: sink.append(True))
    auth.outcome.emit((AuthOutcome.FAILED, auth._current_generation()))
    QCoreApplication.processEvents()
    assert sink == [True]
    assert ctrl.currentText == ""
    assert ctrl.showFailure is True


def test_overlay_key_backspace_pops_char(qapp, auth):
    from qdlocker.keysyms import XKB_BackSpace

    ctrl = LockController(auth)
    ctrl._current_text = "abc"
    ctrl.handle_overlay_key(XKB_BackSpace, "")
    assert ctrl.currentText == "ab"


def test_overlay_key_printable_appends(qapp, auth):
    ctrl = LockController(auth)
    ctrl._current_text = "ab"
    ctrl.handle_overlay_key(0x0078, "x")  # XKB_x
    assert ctrl.currentText == "abx"


def test_overlay_key_return_triggers_unlock(qapp, auth):
    from qdlocker.keysyms import XKB_Return

    ctrl = LockController(auth)
    QCoreApplication.processEvents()  # let queued ready signal land
    ctrl._current_text = "pw"
    ctrl.handle_overlay_key(XKB_Return, "")
    auth.start_pam.assert_called_once()


# ---- item 2: stale-outcome race guard --------------------------------------


def test_tagged_success_unlocks(qapp, auth):
    ctrl = LockController(auth)
    sink = []
    ctrl.unlocked.connect(lambda: sink.append(True))
    # Tagged tuple from the real backend, matching the current generation.
    auth.outcome.emit((AuthOutcome.SUCCESS, auth._current_generation()))
    QCoreApplication.processEvents()
    assert sink == [True]
    assert ctrl._unlocked is True


@pytest.mark.cheat_aware(
    protects="every auth outcome must be tagged (AuthOutcome, generation); a "
    "bare untagged AuthOutcome is dropped so it cannot bypass the anti-replay "
    "generation check",
    severity="critical",
    cheats=[
        "accept a bare untagged AuthOutcome as a valid outcome",
        "emit a tagged tuple instead of testing the bare untagged path",
        "compare generation with <= instead of exact-match equality",
    ],
    consequence="a future/refactored backend's untagged SUCCESS bypasses the "
    "anti-replay generation check and unlocks the screen without the "
    "stale-outcome guard — an unlock-without-auth bypass",
)
def test_untagged_success_is_dropped(qapp, auth):
    ctrl = LockController(auth)
    sink = []
    ctrl.unlocked.connect(lambda: sink.append(True))
    # A bare untagged outcome must never unlock the screen.
    auth.outcome.emit(AuthOutcome.SUCCESS)
    QCoreApplication.processEvents()
    assert sink == [], "untagged SUCCESS leaked through and unlocked"
    assert ctrl._unlocked is False


def test_untagged_failed_is_dropped(qapp, auth):
    ctrl = LockController(auth)
    ctrl._current_text = "wrongpw"
    fails = []
    ctrl.failed.connect(lambda: fails.append(True))
    auth.outcome.emit(AuthOutcome.FAILED)
    QCoreApplication.processEvents()
    assert fails == [], "untagged FAILED flashed a spurious failure"
    assert ctrl.currentText == "wrongpw"
    assert ctrl.showFailure is False


def test_malformed_tuples_are_dropped(qapp, auth):
    ctrl = LockController(auth)
    unlocks = []
    fails = []
    ctrl.unlocked.connect(lambda: unlocks.append(True))
    ctrl.failed.connect(lambda: fails.append(True))
    # Wrong arity, non-int generation, and a bool generation are all
    # malformed and dropped. The bool case matters because bool subclasses
    # int: with the current generation at 0, a stray (SUCCESS, False) would
    # slip past an isinstance(_, int) check (False == 0) and unlock.
    auth.outcome.emit((AuthOutcome.SUCCESS,))
    QCoreApplication.processEvents()
    auth.outcome.emit((AuthOutcome.SUCCESS, "0"))
    QCoreApplication.processEvents()
    assert auth._current_generation() == 0  # so (SUCCESS, False) would match gen 0
    auth.outcome.emit((AuthOutcome.SUCCESS, False))
    QCoreApplication.processEvents()
    assert unlocks == [], "malformed outcome leaked through and unlocked"
    assert fails == []
    assert ctrl._unlocked is False
    assert ctrl.showFailure is False


@pytest.mark.cheat_aware(
    protects="an auth outcome tagged with a superseded session generation "
    "cannot unlock or flash failure on a fresh lock — only a result for "
    "the current generation is honored",
    severity="critical",
    cheats=[
        "emit the outcome with the current generation instead of the old one",
        "skip notify_lock_begin so no generation bump occurs",
        "assert only on showFailure and drop the fails == [] check",
        "compare generations with <= instead of exact-match equality",
    ],
    consequence="a slow/laggy worker's stale SUCCESS from a previous lock "
    "session unlocks the screen for whoever is in front of it now — a "
    "real unlock-without-auth bypass",
)
def test_stale_outcome_from_old_generation_is_dropped(qapp, auth):
    ctrl = LockController(auth)
    captured_gen = auth._current_generation()
    # A new lock begins, bumping the generation, before the slow worker's
    # FAILED finally arrives tagged with the old generation.
    ctrl.notify_lock_begin()
    fails = []
    ctrl.failed.connect(lambda: fails.append(True))
    auth.outcome.emit((AuthOutcome.FAILED, captured_gen))
    QCoreApplication.processEvents()
    assert fails == [], "stale FAILED from a superseded session leaked through"
    assert ctrl.showFailure is False


def test_loser_outcome_after_unlock_is_dropped(qapp, auth):
    ctrl = LockController(auth)
    gen = auth._current_generation()
    unlocks = []
    fails = []
    ctrl.unlocked.connect(lambda: unlocks.append(True))
    ctrl.failed.connect(lambda: fails.append(True))
    # fprintd wins first (same generation), then a laggy PAM FAILED races in.
    auth.outcome.emit((AuthOutcome.SUCCESS, gen))
    QCoreApplication.processEvents()
    auth.outcome.emit((AuthOutcome.FAILED, gen))
    QCoreApplication.processEvents()
    assert unlocks == [True]
    assert fails == [], "spurious FAILED flashed after the session unlocked"
    assert ctrl.showFailure is False


def test_notify_lock_begin_resets_unlocked_and_session(qapp, auth):
    ctrl = LockController(auth)
    ctrl._unlocked = True
    ctrl.notify_lock_begin()
    assert ctrl._unlocked is False
    # reset_session was invoked on the backend (generation advanced).
    assert auth._current_generation() == 1


@pytest.mark.cheat_aware(
    protects="a fresh lock arms the fingerprint sensor immediately, with no "
    "keystroke required, so touch-to-unlock works on an empty password field",
    severity="high",
    cheats=[
        "arm fingerprint only from the currentText setter once text is typed",
        "call occupy_fingerprint_sensor(False) or omit the arm entirely",
        "assert call count without asserting it was armed with True",
    ],
    consequence="an idle lock leaves fprintd dormant; a finger-only unlock "
    "never fires — the touch-to-unlock spec path is dead until the user types, "
    "and GUI scenario 02 hangs",
)
def test_notify_lock_begin_arms_fingerprint(qapp, auth):
    ctrl = LockController(auth)
    auth.occupy_fingerprint_sensor.reset_mock()
    ctrl.notify_lock_begin()
    # Sensor armed on the fresh lock with no text typed.
    auth.occupy_fingerprint_sensor.assert_called_once_with(True)


def test_notify_lock_begin_arms_after_session_reset(qapp, auth):
    """The sensor must be armed AFTER reset_session() so the fprintd worker
    captures the fresh generation (and a prior transient wedge is retried)."""
    ctrl = LockController(auth)
    order = []
    auth.reset_session = MagicMock(side_effect=lambda: order.append("reset"))
    auth.occupy_fingerprint_sensor = MagicMock(
        side_effect=lambda on: order.append(("occupy", on))
    )
    ctrl.notify_lock_begin()
    assert order == ["reset", ("occupy", True)]
