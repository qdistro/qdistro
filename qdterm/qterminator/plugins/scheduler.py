"""Scheduled text plugin for QTerminator.

Schedules text (commands) to be sent to a terminal after a delay,
optionally repeating on an interval. Useful for periodic pings,
status checks, or delayed commands.

Configuration:

```toml
# Schedules are not persisted between restarts (yet)
```
"""

from PyQt6.QtCore import QDateTime, QTimer
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
)

from qterminator.plugin import MenuProvider

TAB_INDICATOR = "\u23f0 "  # alarm clock


class ScheduledTask:
    """A pending (and possibly repeating) scheduled text send."""

    def __init__(self, terminal, text, initial_delay, repeat_delay):
        self.terminal = terminal
        self.text = text
        self.initial_delay = initial_delay  # seconds
        self.repeat_delay = repeat_delay    # seconds; 0 = no repeat
        self.qtimer = QTimer()
        self.qtimer.setSingleShot(True)
        self.next_fire_time = None  # QDateTime

    def start(self, delay_seconds):
        """(Re)start the timer with the given delay."""
        self.next_fire_time = QDateTime.currentDateTime().addSecs(delay_seconds)
        self.qtimer.start(int(delay_seconds * 1000))

    def stop(self):
        self.qtimer.stop()

    def remaining_seconds(self):
        if self.next_fire_time is None:
            return 0
        now = QDateTime.currentDateTime()
        ms = now.msecsTo(self.next_fire_time)
        return max(0, ms // 1000)


class ScheduleDialog(QDialog):
    """Dialog for creating or editing a scheduled task."""

    def __init__(self, parent=None, text="", initial_delay=10, repeat_delay=0):
        super().__init__(parent)
        self.setWindowTitle("Schedule Command")

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.text_edit = QLineEdit(text)
        self.text_edit.setPlaceholderText("e.g. echo hello")
        form.addRow("Command:", self.text_edit)

        self.initial_spin = QSpinBox()
        self.initial_spin.setRange(1, 3600)
        self.initial_spin.setSuffix(" s")
        self.initial_spin.setValue(initial_delay)
        form.addRow("Initial delay:", self.initial_spin)

        self.repeat_spin = QSpinBox()
        self.repeat_spin.setRange(0, 3600)
        self.repeat_spin.setSuffix(" s")
        self.repeat_spin.setSpecialValueText("no repeat")
        self.repeat_spin.setValue(repeat_delay)
        form.addRow("Repeat delay:", self.repeat_spin)

        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self):
        return (
            self.text_edit.text(),
            self.initial_spin.value(),
            self.repeat_spin.value(),
        )


class SchedulerPlugin(MenuProvider):
    """Menu provider that schedules text to be sent to a terminal."""

    name = "scheduler"
    description = "Schedule text to be sent to a terminal after a delay"
    version = "1.0"
    category = "Schedule"

    def __init__(self):
        # terminal_id (id(terminal)) -> ScheduledTask
        self._schedules = {}

    # --- Lifecycle ---

    def deactivate(self):
        """Stop and remove all pending timers."""
        for task in list(self._schedules.values()):
            task.stop()
        self._schedules.clear()

    # --- MenuProvider ---

    def get_menu_items(self, terminal):
        items = [
            ("Schedule Command...",
             lambda t=terminal: self._open_schedule_dialog(t)),
        ]
        if id(terminal) in self._schedules:
            items.append(("Cancel Schedule",
                          lambda t=terminal: self.cancel(t)))
            items.append(("Trigger Now",
                          lambda t=terminal: self.trigger_now(t)))
            items.append(("Edit Schedule",
                          lambda t=terminal: self._edit_schedule(t)))
            items.append(("Show Remaining Time",
                          lambda t=terminal: self._show_remaining(t)))
        return items

    # --- Public API ---

    def schedule(self, terminal, text, initial_delay, repeat_delay=0):
        """Create (or replace) a scheduled task for `terminal`."""
        # Replace any existing schedule
        self.cancel(terminal)

        task = ScheduledTask(terminal, text, initial_delay, repeat_delay)
        task.qtimer.timeout.connect(lambda t=terminal: self._fire(t))
        self._schedules[id(terminal)] = task
        task.start(initial_delay)
        self._update_tab_indicator(terminal)
        return task

    def cancel(self, terminal):
        """Cancel any scheduled task for `terminal`."""
        task = self._schedules.pop(id(terminal), None)
        if task is not None:
            task.stop()
            self._update_tab_indicator(terminal)
            return True
        return False

    def trigger_now(self, terminal):
        """Fire the scheduled task for `terminal` immediately."""
        task = self._schedules.get(id(terminal))
        if task is None:
            return False
        task.stop()
        self._fire(terminal)
        return True

    def get_task(self, terminal):
        """Return the ScheduledTask for `terminal`, or None."""
        return self._schedules.get(id(terminal))

    # --- Internals ---

    def _fire(self, terminal):
        task = self._schedules.get(id(terminal))
        if task is None:
            return
        try:
            terminal.send_text(task.text + "\n")
        except Exception:
            # Terminal may have been closed
            self._schedules.pop(id(terminal), None)
            return

        if task.repeat_delay > 0:
            task.start(task.repeat_delay)
        else:
            self._schedules.pop(id(terminal), None)
            self._update_tab_indicator(terminal)

    def _open_schedule_dialog(self, terminal):
        existing = self._schedules.get(id(terminal))
        if existing is not None:
            dlg = ScheduleDialog(
                terminal, text=existing.text,
                initial_delay=existing.initial_delay,
                repeat_delay=existing.repeat_delay,
            )
        else:
            dlg = ScheduleDialog(terminal)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            text, initial, repeat = dlg.values()
            if text.strip():
                self.schedule(terminal, text, initial, repeat)

    def _edit_schedule(self, terminal):
        self._open_schedule_dialog(terminal)

    def _show_remaining(self, terminal):
        task = self._schedules.get(id(terminal))
        if task is None:
            return
        secs = task.remaining_seconds()
        repeat = (f"; repeats every {task.repeat_delay}s"
                  if task.repeat_delay > 0 else "")
        QMessageBox.information(
            terminal, "Scheduled Command",
            f"Next fire in {secs}s{repeat}\nCommand: {task.text}",
        )

    # --- Tab indicator ---

    def _update_tab_indicator(self, terminal):
        """Add/remove an alarm-clock prefix on the tab containing `terminal`."""
        try:
            window = terminal.window()
        except Exception:
            return
        tabs = getattr(window, "_tabs", None)
        if tabs is None:
            return
        active = id(terminal) in self._schedules
        for i in range(tabs.count()):
            split = tabs.widget(i)
            if split is None or not hasattr(split, "find_terminals"):
                continue
            if terminal not in split.find_terminals():
                continue
            current = tabs.tabText(i)
            has_indicator = current.startswith(TAB_INDICATOR)
            if active and not has_indicator:
                tabs.setTabText(i, TAB_INDICATOR + current)
            elif not active and has_indicator:
                tabs.setTabText(i, current[len(TAB_INDICATOR):])
            break
