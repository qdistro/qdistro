"""Tests for the scheduler plugin."""

import pytest
import qterminator.config as config_mod
from qterminator.config import Config
from qterminator.plugins.scheduler import (
    TAB_INDICATOR,
    ScheduledTask,
    SchedulerPlugin,
)


@pytest.fixture(autouse=True)
def fresh_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp_path / "config.toml"))
    Config._instance = None
    yield
    Config._instance = None


class FakeTerminal:
    """Minimal terminal stand-in that records send_text calls."""

    def __init__(self):
        self.sent = []

    def send_text(self, text):
        self.sent.append(text)

    def window(self):
        return None  # no tab indicator updates in unit tests


# --- Plugin basics -----------------------------------------------------------

def test_scheduler_plugin_metadata():
    p = SchedulerPlugin()
    assert p.name == "scheduler"
    assert "menu_provider" in p.capabilities


def test_menu_items_no_schedule():
    """With no schedule for a terminal, only 'Schedule Command...' is offered."""
    p = SchedulerPlugin()
    t = FakeTerminal()
    items = p.get_menu_items(t)
    labels = [label for label, _ in items]
    assert labels == ["Schedule Command..."]


def test_menu_items_with_schedule(qtbot):
    """With an active schedule, cancel/trigger/edit/remaining items appear."""
    p = SchedulerPlugin()
    t = FakeTerminal()
    p.schedule(t, "ls", initial_delay=60, repeat_delay=0)
    labels = [label for label, _ in p.get_menu_items(t)]
    assert "Schedule Command..." in labels
    assert "Cancel Schedule" in labels
    assert "Trigger Now" in labels
    assert "Edit Schedule" in labels
    assert "Show Remaining Time" in labels
    p.deactivate()


# --- Schedule creation -------------------------------------------------------

def test_schedule_creation(qtbot):
    p = SchedulerPlugin()
    t = FakeTerminal()
    task = p.schedule(t, "echo hi", initial_delay=5, repeat_delay=0)
    assert isinstance(task, ScheduledTask)
    assert p.get_task(t) is task
    assert task.text == "echo hi"
    assert task.initial_delay == 5
    assert task.repeat_delay == 0
    assert task.qtimer.isActive()
    p.deactivate()


def test_schedule_replaces_existing(qtbot):
    """Scheduling twice replaces the previous schedule."""
    p = SchedulerPlugin()
    t = FakeTerminal()
    first = p.schedule(t, "one", initial_delay=60)
    second = p.schedule(t, "two", initial_delay=60)
    assert first is not second
    assert p.get_task(t) is second
    assert not first.qtimer.isActive()
    p.deactivate()


# --- Firing ------------------------------------------------------------------

def test_initial_delay_fires(qtbot):
    """Task fires after the initial delay elapses."""
    p = SchedulerPlugin()
    t = FakeTerminal()
    # Use a very small delay via the timer directly
    task = p.schedule(t, "cmd", initial_delay=1, repeat_delay=0)
    # Shortcut: stop the real 1-second timer and fire immediately
    task.qtimer.stop()
    task.qtimer.start(50)  # 50 ms
    qtbot.wait(200)
    assert t.sent == ["cmd\n"]
    # Non-repeating: removed from active schedules
    assert p.get_task(t) is None


def test_repeat_fires_multiple_times(qtbot):
    """Repeating task fires more than once."""
    p = SchedulerPlugin()
    t = FakeTerminal()
    task = p.schedule(t, "tick", initial_delay=1, repeat_delay=1)
    task.qtimer.stop()
    # Swap delays to small ms for fast testing
    task.initial_delay = 1
    task.repeat_delay = 1  # seconds; the _fire code uses this for restart
    # Restart with 50 ms first
    task.qtimer.start(50)
    # Monkey-patch repeat_delay to a short value by replacing _fire? Instead,
    # patch the task's repeat_delay to fractional seconds via a subclass trick:
    # The _fire method uses task.start(task.repeat_delay) which passes to
    # QTimer.start(int(seconds * 1000)). So set repeat_delay=0.05 (int ok? No,
    # int() truncates to 0). We need to patch start() for this test.
    def fast_start(delay_seconds):
        # Use 50 ms regardless of requested delay
        from PyQt6.QtCore import QDateTime
        task.next_fire_time = QDateTime.currentDateTime().addMSecs(50)
        task.qtimer.start(50)

    task.start = fast_start
    # Let it fire at least 3 times
    qtbot.wait(300)
    assert len(t.sent) >= 2
    assert all(s == "tick\n" for s in t.sent)
    p.cancel(t)


# --- Cancel ------------------------------------------------------------------

def test_cancel_removes_schedule(qtbot):
    p = SchedulerPlugin()
    t = FakeTerminal()
    task = p.schedule(t, "cmd", initial_delay=60)
    assert p.cancel(t) is True
    assert p.get_task(t) is None
    assert not task.qtimer.isActive()


def test_cancel_nonexistent_returns_false():
    p = SchedulerPlugin()
    t = FakeTerminal()
    assert p.cancel(t) is False


def test_cancelled_task_does_not_fire(qtbot):
    p = SchedulerPlugin()
    t = FakeTerminal()
    task = p.schedule(t, "cmd", initial_delay=1)
    task.qtimer.stop()
    task.qtimer.start(50)
    p.cancel(t)
    qtbot.wait(150)
    assert t.sent == []


# --- Trigger now -------------------------------------------------------------

def test_trigger_now_fires_immediately(qtbot):
    p = SchedulerPlugin()
    t = FakeTerminal()
    p.schedule(t, "now", initial_delay=3600, repeat_delay=0)
    assert p.trigger_now(t) is True
    assert t.sent == ["now\n"]
    # Non-repeating: schedule removed after firing
    assert p.get_task(t) is None


def test_trigger_now_nonexistent_returns_false():
    p = SchedulerPlugin()
    t = FakeTerminal()
    assert p.trigger_now(t) is False


def test_trigger_now_with_repeat_restarts(qtbot):
    """Triggering a repeating task fires once and restarts the timer."""
    p = SchedulerPlugin()
    t = FakeTerminal()
    p.schedule(t, "ping", initial_delay=3600, repeat_delay=3600)
    p.trigger_now(t)
    assert t.sent == ["ping\n"]
    # Still scheduled for next fire
    task = p.get_task(t)
    assert task is not None
    assert task.qtimer.isActive()
    p.cancel(t)


# --- Multiple schedules ------------------------------------------------------

def test_multiple_schedules_per_window(qtbot):
    """Each terminal has its own independent schedule."""
    p = SchedulerPlugin()
    t1 = FakeTerminal()
    t2 = FakeTerminal()
    p.schedule(t1, "one", initial_delay=60)
    p.schedule(t2, "two", initial_delay=60)
    assert p.get_task(t1).text == "one"
    assert p.get_task(t2).text == "two"

    # Cancelling one leaves the other
    p.cancel(t1)
    assert p.get_task(t1) is None
    assert p.get_task(t2) is not None
    p.deactivate()


def test_trigger_now_affects_only_target(qtbot):
    p = SchedulerPlugin()
    t1 = FakeTerminal()
    t2 = FakeTerminal()
    p.schedule(t1, "one", initial_delay=3600)
    p.schedule(t2, "two", initial_delay=3600)
    p.trigger_now(t1)
    assert t1.sent == ["one\n"]
    assert t2.sent == []
    p.deactivate()


# --- Deactivate --------------------------------------------------------------

def test_deactivate_cleans_up_timers(qtbot):
    p = SchedulerPlugin()
    t1 = FakeTerminal()
    t2 = FakeTerminal()
    task1 = p.schedule(t1, "a", initial_delay=60)
    task2 = p.schedule(t2, "b", initial_delay=60)
    p.deactivate()
    assert p.get_task(t1) is None
    assert p.get_task(t2) is None
    assert not task1.qtimer.isActive()
    assert not task2.qtimer.isActive()


# --- Tab indicator -----------------------------------------------------------

def test_tab_indicator_added_and_removed(qtbot):
    """The tab title gets an alarm-clock prefix when a schedule is active."""
    from qterminator.window import MainWindow
    win = MainWindow()
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)

    split = win._tabs.widget(0)
    terminal = split.find_terminals()[0]
    original_title = win._tabs.tabText(0)

    p = SchedulerPlugin()
    p.schedule(terminal, "cmd", initial_delay=3600, repeat_delay=0)
    assert win._tabs.tabText(0).startswith(TAB_INDICATOR)

    p.cancel(terminal)
    assert not win._tabs.tabText(0).startswith(TAB_INDICATOR)
    assert win._tabs.tabText(0) == original_title

    p.deactivate()


# --- ScheduledTask -----------------------------------------------------------

def test_scheduled_task_remaining_seconds(qtbot):
    t = FakeTerminal()
    task = ScheduledTask(t, "cmd", initial_delay=10, repeat_delay=0)
    task.start(10)
    remaining = task.remaining_seconds()
    # Should be close to 10 (allow for a second of jitter)
    assert 8 <= remaining <= 10
    task.stop()
