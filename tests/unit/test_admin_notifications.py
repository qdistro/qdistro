"""Tests for admin app notification surface.

Covers: NotificationManager (mute, pending tracking, desktop notifications,
age display), helper functions (_format_age, _age_color), and the
integration points on MainWindow (window title, tray badge sync).

These tests require PyQt6 for QObject/QTimer/QSystemTrayIcon but do NOT
require a live D-Bus session or a running broker — all broker interaction
is stubbed.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Skip the entire module when PyQt6 is not importable.
QtWidgets = pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)
QtCore = pytest.importorskip("PyQt6.QtCore", exc_type=ImportError)

from PyQt6.QtWidgets import QApplication, QSystemTrayIcon  # noqa: E402
from PyQt6.QtCore import QTimer  # noqa: E402

# Make the admin_app package importable.
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "admin_app"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Import after path setup.
from qdistro_admin_app import (  # noqa: E402
    NotificationManager,
    MUTE_DURATION_S,
    ESCALATION_THRESHOLD_S,
    CRITICAL_THRESHOLD_S,
    CROSS_UID_THRESHOLD_S,
    ESCALATION_MAX_PER_TICK,
    _format_age,
    _age_color,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def qapp():
    """Singleton QApplication for the test session.

    PyQt6 requires exactly one QApplication per process; creating a second
    raises RuntimeError. scope="session" avoids that.
    """
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    return app


@pytest.fixture()
def tray(qapp):
    """A QSystemTrayIcon with showMessage mocked out."""
    icon = QSystemTrayIcon()
    icon.showMessage = MagicMock()
    return icon


@pytest.fixture()
def mgr(tray):
    """A NotificationManager wired to the mocked tray icon."""
    m = NotificationManager(tray)
    # Stop the age timer so it doesn't fire during tests.
    m._age_timer.stop()
    return m


# ---------------------------------------------------------------------------
# _format_age
# ---------------------------------------------------------------------------

class TestFormatAge:
    def test_zero_seconds(self):
        assert _format_age(time.time()) == "0s"

    def test_under_one_minute(self):
        assert _format_age(time.time() - 45) == "45s"

    def test_exactly_one_minute(self):
        assert _format_age(time.time() - 60) == "1m"

    def test_minutes_and_seconds(self):
        assert _format_age(time.time() - 130) == "2m10s"

    def test_exactly_one_hour(self):
        assert _format_age(time.time() - 3600) == "1h"

    def test_hours_and_minutes(self):
        assert _format_age(time.time() - 3720) == "1h2m"

    def test_future_epoch_clamps_to_zero(self):
        """If epoch_s is in the future (clock skew), clamp to 0s."""
        assert _format_age(time.time() + 100) == "0s"


# ---------------------------------------------------------------------------
# _age_color
# ---------------------------------------------------------------------------

class TestAgeColor:
    def test_green_under_30s(self):
        c = _age_color(time.time() - 10)
        assert c.name() == "#4caf50"

    def test_yellow_30s_to_2m(self):
        c = _age_color(time.time() - 60)
        assert c.name() == "#ffeb3b"

    def test_orange_2m_to_5m(self):
        c = _age_color(time.time() - 180)
        assert c.name() == "#ff9800"

    def test_red_over_5m(self):
        c = _age_color(time.time() - 400)
        assert c.name() == "#f44336"


# ---------------------------------------------------------------------------
# NotificationManager — mute
# ---------------------------------------------------------------------------

class TestMute:
    def test_initially_not_muted(self, mgr):
        assert mgr.muted is False

    def test_mute_sets_flag(self, mgr):
        mgr.mute(duration_s=60)
        assert mgr.muted is True

    def test_unmute_clears_flag(self, mgr):
        mgr.mute(duration_s=60)
        mgr.unmute()
        assert mgr.muted is False

    def test_auto_unmute_after_timer(self, mgr, qapp):
        """Mute with a very short duration; process events; verify unmute."""
        mgr.mute(duration_s=0)  # 0 seconds → timer fires immediately
        # Process pending events so the QTimer fires.
        qapp.processEvents()
        # QTimer with interval=0 fires on next event loop tick.
        QTimer.singleShot(0, lambda: None)
        qapp.processEvents()
        # Give it a bit more time for the single-shot to fire.
        for _ in range(10):
            qapp.processEvents()
        assert mgr.muted is False

    def test_mute_timer_is_single_shot(self, mgr):
        mgr.mute(duration_s=300)
        assert mgr._mute_timer.isSingleShot()


# ---------------------------------------------------------------------------
# NotificationManager — pending tracking
# ---------------------------------------------------------------------------

class TestPendingTracking:
    def test_initial_count_zero(self, mgr):
        assert mgr.pending_count == 0

    def test_on_request_pending_increments(self, mgr):
        mgr.on_request_pending(1)
        assert mgr.pending_count == 1
        mgr.on_request_pending(2)
        assert mgr.pending_count == 2

    def test_on_request_decided_decrements(self, mgr):
        mgr.on_request_pending(1)
        mgr.on_request_pending(2)
        mgr.on_request_decided(1)
        assert mgr.pending_count == 1

    def test_decided_unknown_rid_is_noop(self, mgr):
        mgr.on_request_pending(1)
        mgr.on_request_decided(999)
        assert mgr.pending_count == 1

    def test_duplicate_pending_does_not_double_count(self, mgr):
        mgr.on_request_pending(1)
        mgr.on_request_pending(1)
        assert mgr.pending_count == 1

    def test_arrival_time_recorded(self, mgr):
        before = time.time()
        mgr.on_request_pending(42)
        after = time.time()
        t = mgr.arrival_time(42)
        assert t is not None
        assert before <= t <= after

    def test_arrival_time_none_for_unknown(self, mgr):
        assert mgr.arrival_time(999) is None

    def test_sync_pending_adds_missing(self, mgr):
        mgr.sync_pending({10, 20, 30})
        assert mgr.pending_count == 3
        assert mgr.arrival_time(10) is not None
        assert mgr.arrival_time(20) is not None
        assert mgr.arrival_time(30) is not None

    def test_sync_pending_removes_stale(self, mgr):
        mgr.on_request_pending(1)
        mgr.on_request_pending(2)
        mgr.on_request_pending(3)
        mgr.sync_pending({2})
        assert mgr.pending_count == 1
        assert mgr.arrival_time(1) is None
        assert mgr.arrival_time(3) is None
        assert mgr.arrival_time(2) is not None

    def test_sync_preserves_existing_timestamps(self, mgr):
        mgr.on_request_pending(5)
        t_before = mgr.arrival_time(5)
        # Small delay so any new timestamp would differ.
        mgr.sync_pending({5, 6})
        assert mgr.arrival_time(5) == t_before  # not overwritten
        assert mgr.arrival_time(6) is not None   # newly added


# ---------------------------------------------------------------------------
# NotificationManager — desktop notification
# ---------------------------------------------------------------------------

class TestDesktopNotification:
    def test_notify_when_window_not_active_and_not_muted(self, mgr, tray):
        mgr.maybe_notify("qdistro.exec", 2000, window_active=False)
        tray.showMessage.assert_called_once()
        args = tray.showMessage.call_args
        assert "Pending approval request" in args[0][0]
        assert "uid=2000" in args[0][1]
        assert "qdistro.exec" in args[0][1]

    def test_no_notify_when_window_active(self, mgr, tray):
        mgr.maybe_notify("qdistro.exec", 2000, window_active=True)
        tray.showMessage.assert_not_called()

    def test_no_notify_when_muted(self, mgr, tray):
        mgr.mute(duration_s=300)
        mgr.maybe_notify("qdistro.exec", 2000, window_active=False)
        tray.showMessage.assert_not_called()

    def test_no_notify_when_muted_and_window_active(self, mgr, tray):
        mgr.mute(duration_s=300)
        mgr.maybe_notify("qdistro.exec", 2000, window_active=True)
        tray.showMessage.assert_not_called()

    def test_notify_after_unmute(self, mgr, tray):
        mgr.mute(duration_s=300)
        mgr.unmute()
        mgr.maybe_notify("qdistro.exec", 2000, window_active=False)
        tray.showMessage.assert_called_once()


# ---------------------------------------------------------------------------
# NotificationManager — age timer
# ---------------------------------------------------------------------------

class TestAgeTimer:
    def test_age_timer_interval(self, mgr):
        """Age timer should fire every 10 seconds."""
        assert mgr._age_timer.interval() == 10_000

    def test_age_tick_signal_emitted(self, mgr, qapp):
        """Manually fire the age timer and verify the signal."""
        received = []
        mgr.ageTickFired.connect(lambda: received.append(True))
        mgr.ageTickFired.emit()
        assert len(received) == 1


# ---------------------------------------------------------------------------
# NotificationManager — escalation
# ---------------------------------------------------------------------------

class TestEscalation:
    """Threshold-based re-notification for stale pending requests."""

    def test_no_escalation_before_threshold(self, mgr, tray):
        """Requests younger than the threshold should not escalate."""
        mgr.on_request_pending(1, action="qdistro.exec", uid=2000)
        mgr._check_escalation(window_active=False)
        tray.showMessage.assert_not_called()

    def test_escalation_after_threshold(self, mgr, tray):
        """Request pending >60s gets a re-notification."""
        mgr.on_request_pending(1, action="qdistro.exec", uid=2000)
        # Backdate arrival to trigger standard escalation.
        mgr._arrival_times[1] = time.time() - 70
        mgr._check_escalation(window_active=False)
        tray.showMessage.assert_called_once()
        args = tray.showMessage.call_args
        assert "Reminder" in args[0][0]
        assert "uid=2000" in args[0][1]
        assert args[0][2] == QSystemTrayIcon.MessageIcon.Information

    def test_critical_escalation_after_300s(self, mgr, tray):
        """Request pending >300s gets a critical-level notification."""
        mgr.on_request_pending(1, action="qdistro.exec", uid=2000)
        mgr._arrival_times[1] = time.time() - 310
        mgr._check_escalation(window_active=False)
        tray.showMessage.assert_called_once()
        args = tray.showMessage.call_args
        assert "CRITICAL" in args[0][0]
        assert args[0][2] == QSystemTrayIcon.MessageIcon.Critical

    def test_cross_uid_escalates_faster(self, mgr, tray):
        """Cross-uid actions escalate at CROSS_UID_THRESHOLD_S (30s)."""
        mgr.on_request_pending(1, action="qdistro.cross_clipboard", uid=2000)
        # 35s is past 30s cross-uid threshold but under 60s standard.
        mgr._arrival_times[1] = time.time() - 35
        mgr._check_escalation(window_active=False)
        tray.showMessage.assert_called_once()
        assert "Reminder" in tray.showMessage.call_args[0][0]

    def test_xuid_action_also_escalates_faster(self, mgr, tray):
        """Actions containing 'xuid' also use the cross-uid threshold."""
        mgr.on_request_pending(1, action="qdistro.xuid_paste", uid=2000)
        mgr._arrival_times[1] = time.time() - 35
        mgr._check_escalation(window_active=False)
        tray.showMessage.assert_called_once()

    def test_no_double_escalation(self, mgr, tray):
        """Once escalated, the same rid should not re-fire on the next tick."""
        mgr.on_request_pending(1, action="qdistro.exec", uid=2000)
        mgr._arrival_times[1] = time.time() - 70
        mgr._check_escalation(window_active=False)
        assert tray.showMessage.call_count == 1
        mgr._check_escalation(window_active=False)
        assert tray.showMessage.call_count == 1  # still 1

    def test_no_double_critical(self, mgr, tray):
        """Critical notification fires at most once per rid."""
        mgr.on_request_pending(1, action="qdistro.exec", uid=2000)
        mgr._arrival_times[1] = time.time() - 310
        mgr._check_escalation(window_active=False)
        assert tray.showMessage.call_count == 1
        mgr._check_escalation(window_active=False)
        assert tray.showMessage.call_count == 1

    def test_escalation_suppressed_when_muted(self, mgr, tray):
        """Muted state suppresses escalation too."""
        mgr.on_request_pending(1, action="qdistro.exec", uid=2000)
        mgr._arrival_times[1] = time.time() - 70
        mgr.mute(duration_s=600)
        mgr._check_escalation(window_active=False)
        tray.showMessage.assert_not_called()

    def test_escalation_suppressed_when_window_active(self, mgr, tray):
        """No escalation if the admin window is focused."""
        mgr.on_request_pending(1, action="qdistro.exec", uid=2000)
        mgr._arrival_times[1] = time.time() - 70
        mgr._check_escalation(window_active=True)
        tray.showMessage.assert_not_called()

    def test_decided_clears_escalation_state(self, mgr, tray):
        """Deciding a request clears its escalation tracking."""
        mgr.on_request_pending(1, action="qdistro.exec", uid=2000)
        mgr._arrival_times[1] = time.time() - 70
        mgr._check_escalation(window_active=False)
        assert 1 in mgr._escalated_rids
        mgr.on_request_decided(1)
        assert 1 not in mgr._escalated_rids
        assert 1 not in mgr._critical_rids

    def test_sync_pending_clears_stale_escalation(self, mgr, tray):
        """sync_pending removes escalation state for stale rids."""
        mgr.on_request_pending(1, action="qdistro.exec", uid=2000)
        mgr._arrival_times[1] = time.time() - 70
        mgr._check_escalation(window_active=False)
        assert 1 in mgr._escalated_rids
        # Sync with empty set — request 1 is gone.
        mgr.sync_pending(set())
        assert 1 not in mgr._escalated_rids
        assert mgr.pending_count == 0

    def test_multiple_requests_escalate_independently(self, mgr, tray):
        """Each pending request is tracked independently for escalation."""
        mgr.on_request_pending(1, action="qdistro.exec.ls", uid=2000)
        mgr.on_request_pending(2, action="qdistro.exec.cat", uid=2001)
        # Only backdate request 1.
        mgr._arrival_times[1] = time.time() - 70
        mgr._check_escalation(window_active=False)
        assert tray.showMessage.call_count == 1
        assert 1 in mgr._escalated_rids
        assert 2 not in mgr._escalated_rids

    def test_is_cross_uid_action_positive(self):
        assert NotificationManager._is_cross_uid_action("qdistro.cross_clipboard")
        assert NotificationManager._is_cross_uid_action("qdistro.xuid_paste")
        assert NotificationManager._is_cross_uid_action("CROSS_UID_RW")

    def test_is_cross_uid_action_real_broker_clipboard_transfer(self):
        """Real broker action qdistro.clipboard.transfer:<src>:<dst>."""
        assert NotificationManager._is_cross_uid_action(
            "qdistro.clipboard.transfer:user1:user2")

    def test_is_cross_uid_action_real_broker_clipboard_receive(self):
        """Real broker action qdistro.clipboard.receive:<src>:<dst>."""
        assert NotificationManager._is_cross_uid_action(
            "qdistro.clipboard.receive:admin:user1")

    def test_is_cross_uid_action_real_broker_app_send_to(self):
        """Real broker action app.send-to:<uid>:<service>."""
        assert NotificationManager._is_cross_uid_action(
            "app.send-to:2001:org.example.Service")

    def test_is_cross_uid_action_qsu_exec(self):
        """qsu.exec:<target_user> is a sensitive delegated action."""
        assert NotificationManager._is_cross_uid_action("qsu.exec:root")
        assert NotificationManager._is_cross_uid_action("qsu.exec:nobody")

    def test_is_cross_uid_action_handoff_activate(self):
        """qdistro.handoff.activate:<src>:<dst> is cross-silo."""
        assert NotificationManager._is_cross_uid_action(
            "qdistro.handoff.activate:user1:user2")

    def test_is_cross_uid_action_negative(self):
        assert not NotificationManager._is_cross_uid_action("qdistro.exec.ls")
        assert not NotificationManager._is_cross_uid_action("")

    def test_on_request_pending_stores_metadata(self, mgr):
        mgr.on_request_pending(42, action="qdistro.exec", uid=1000)
        assert mgr._request_meta[42] == {"action": "qdistro.exec", "uid": 1000}

    def test_on_request_decided_clears_metadata(self, mgr):
        mgr.on_request_pending(42, action="qdistro.exec", uid=1000)
        mgr.on_request_decided(42)
        assert 42 not in mgr._request_meta

    def test_sync_pending_backfills_metadata(self, mgr):
        """sync_pending with request dicts backfills action/uid metadata."""
        mgr.sync_pending(
            {10, 20},
            pending_requests=[
                {"id": 10, "uid": 2000, "action": "qdistro.exec.ls"},
                {"id": 20, "uid": 2001, "action": "qdistro.clipboard.transfer:a:b"},
            ],
        )
        assert mgr._request_meta[10] == {"action": "qdistro.exec.ls", "uid": 2000}
        assert mgr._request_meta[20] == {
            "action": "qdistro.clipboard.transfer:a:b", "uid": 2001}

    def test_sync_pending_does_not_overwrite_existing_metadata(self, mgr):
        """Existing metadata should not be replaced by sync_pending."""
        mgr.on_request_pending(10, action="original", uid=1000)
        mgr.sync_pending(
            {10},
            pending_requests=[
                {"id": 10, "uid": 2000, "action": "replaced"},
            ],
        )
        assert mgr._request_meta[10] == {"action": "original", "uid": 1000}

    def test_escalation_burst_cap(self, mgr, tray):
        """At most ESCALATION_MAX_PER_TICK notifications per tick."""
        # Create more requests than the per-tick cap, all past threshold.
        for i in range(ESCALATION_MAX_PER_TICK + 3):
            mgr.on_request_pending(i, action=f"qdistro.exec.{i}", uid=2000 + i)
            mgr._arrival_times[i] = time.time() - 70
        mgr._check_escalation(window_active=False)
        assert tray.showMessage.call_count == ESCALATION_MAX_PER_TICK
        # Next tick picks up the remaining.
        mgr._check_escalation(window_active=False)
        assert tray.showMessage.call_count == ESCALATION_MAX_PER_TICK + 3

    def test_escalation_threshold_constants(self):
        """Verify the module-level threshold constants."""
        assert ESCALATION_THRESHOLD_S == 60
        assert CRITICAL_THRESHOLD_S == 300
        assert CROSS_UID_THRESHOLD_S == 30
        assert ESCALATION_MAX_PER_TICK == 3


# ---------------------------------------------------------------------------
# MainWindow — window title update
# ---------------------------------------------------------------------------

def _make_stub_broker():
    """Return a MagicMock that quacks like BrokerBridge."""
    broker = MagicMock()
    broker.get_pending.return_value = []
    broker.list_rules.return_value = []
    broker.list_history.return_value = []
    broker.list_cache.return_value = []
    broker.rulesReloaded = MagicMock()
    broker.rulesReloaded.connect = MagicMock()
    broker.requestPending = MagicMock()
    broker.requestPending.connect = MagicMock()
    broker.requestDecided = MagicMock()
    broker.requestDecided.connect = MagicMock()
    broker.approvalRevoked = MagicMock()
    broker.approvalRevoked.connect = MagicMock()
    return broker


class TestWindowTitle:
    """Window title reflects pending count."""

    def test_no_pending_default_title(self, qapp):
        from qdistro_admin_app import MainWindow
        broker = _make_stub_broker()
        win = MainWindow(broker)
        assert win.windowTitle() == "admin approvals"

    def test_pending_count_in_title(self, qapp):
        from qdistro_admin_app import MainWindow
        broker = _make_stub_broker()
        broker.get_pending.return_value = [
            {"id": 1, "uid": 2000, "pid": 100, "exe": "/bin/ls",
             "action": "qdistro.exec.ls", "details": {}},
            {"id": 2, "uid": 2001, "pid": 101, "exe": "/bin/cat",
             "action": "qdistro.exec.cat", "details": {}},
        ]
        win = MainWindow(broker)
        assert "(2 pending)" in win.windowTitle()

    def test_title_reverts_when_all_decided(self, qapp):
        from qdistro_admin_app import MainWindow
        broker = _make_stub_broker()
        broker.get_pending.return_value = [
            {"id": 1, "uid": 2000, "pid": 100, "exe": "/bin/ls",
             "action": "qdistro.exec.ls", "details": {}},
        ]
        win = MainWindow(broker)
        assert "(1 pending)" in win.windowTitle()

        # Simulate all requests decided.
        broker.get_pending.return_value = []
        win.refresh()
        assert win.windowTitle() == "admin approvals"
