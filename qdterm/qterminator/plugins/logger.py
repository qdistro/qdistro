"""Logger plugin for QTerminator.

Saves terminal output to a log file.
"""

import os
from datetime import datetime

from qterminator.plugin import OutputWatcher


class LoggerPlugin(OutputWatcher):
    name = "logger"
    description = "Save terminal output to a log file"
    version = "1.0"

    def __init__(self):
        super().__init__()
        self._log_files = {}  # terminal -> file handle

    def start_logging(self, terminal, log_path=None):
        """Start logging a terminal's output to a file."""
        if terminal in self._log_files:
            return
        if log_path is None:
            log_dir = os.path.expanduser("~/.config/qterminator/logs")
            os.makedirs(log_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            pid = terminal.shell_pid()
            log_path = os.path.join(log_dir, f"terminal_{pid}_{timestamp}.log")
        fh = open(log_path, "a")
        self._log_files[terminal] = fh
        fh.write(f"# Log started at {datetime.now().isoformat()}\n")

    def stop_logging(self, terminal):
        """Stop logging a terminal."""
        fh = self._log_files.pop(terminal, None)
        if fh:
            fh.write(f"\n# Log ended at {datetime.now().isoformat()}\n")
            fh.close()

    def on_output(self, terminal, text):
        fh = self._log_files.get(terminal)
        if fh:
            fh.write(text)
            fh.flush()

    def deactivate(self):
        for fh in self._log_files.values():
            fh.close()
        self._log_files.clear()
