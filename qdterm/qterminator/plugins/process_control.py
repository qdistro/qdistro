"""Process control plugin — suspend, resume, and signal processes.

Adds context menu items to send signals to the foreground process
running in the terminal (SIGSTOP, SIGCONT, SIGTERM, SIGKILL, etc.).

Also provides a "Process Info" item showing PID, command, and state.
"""

import os
import signal

from PyQt6.QtWidgets import QMessageBox

from qterminator.plugin import MenuProvider


class ProcessControlPlugin(MenuProvider):
    """Send signals to the foreground process from the context menu."""

    name = "process_control"
    description = "Suspend, resume, and signal foreground processes"
    version = "1.0"
    category = "Process"
    capabilities = ["menu_provider"]

    def get_menu_items(self, terminal):
        items = []

        fg_pid = terminal.foreground_pid()
        shell_pid = terminal.shell_pid()
        has_process = fg_pid > 0 and fg_pid != shell_pid

        if has_process:
            # Show process info
            proc_name = self._get_process_name(fg_pid)
            items.append((
                f"Process: {proc_name} (PID {fg_pid})",
                lambda: self._show_process_info(terminal, fg_pid),
            ))
            items.append((
                "Suspend Process (SIGSTOP)",
                lambda pid=fg_pid: self._send_signal(terminal, pid, signal.SIGSTOP, "Suspended"),
            ))
            items.append((
                "Resume Process (SIGCONT)",
                lambda pid=fg_pid: self._send_signal(terminal, pid, signal.SIGCONT, "Resumed"),
            ))
            items.append(("---", None))  # separator hint
            items.append((
                "Interrupt (SIGINT)",
                lambda pid=fg_pid: self._send_signal(terminal, pid, signal.SIGINT, "Interrupted"),
            ))
            items.append((
                "Terminate (SIGTERM)",
                lambda pid=fg_pid: self._send_signal(terminal, pid, signal.SIGTERM, "Terminated"),
            ))
            items.append((
                "Kill (SIGKILL)",
                lambda pid=fg_pid: self._confirm_and_kill(terminal, pid),
            ))
            items.append(("---", None))
            items.append((
                "Send to Background (SIGTSTP)",
                lambda pid=fg_pid: self._send_signal(terminal, pid, signal.SIGTSTP, "Backgrounded"),
            ))
        else:
            items.append((
                "No foreground process",
                lambda: None,
            ))
            # Still allow resuming in case something was stopped
            items.append((
                "Resume All (SIGCONT to shell group)",
                lambda: self._resume_group(terminal),
            ))

        return items

    def _get_process_name(self, pid):
        """Get process name from /proc."""
        try:
            with open(f"/proc/{pid}/comm") as f:
                return f.read().strip()
        except (FileNotFoundError, PermissionError):
            return "unknown"

    def _get_process_info(self, pid):
        """Get detailed process info."""
        info = {"pid": pid}
        try:
            with open(f"/proc/{pid}/comm") as f:
                info["name"] = f.read().strip()
        except (FileNotFoundError, PermissionError):
            info["name"] = "unknown"
        try:
            with open(f"/proc/{pid}/cmdline") as f:
                info["cmdline"] = f.read().replace('\0', ' ').strip()
        except (FileNotFoundError, PermissionError):
            info["cmdline"] = ""
        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("State:"):
                        info["state"] = line.split(":", 1)[1].strip()
                    elif line.startswith("VmRSS:"):
                        info["rss"] = line.split(":", 1)[1].strip()
        except (FileNotFoundError, PermissionError):
            pass
        try:
            with open(f"/proc/{pid}/stat") as f:
                fields = f.read().split()
                # utime + stime in clock ticks
                ticks = int(fields[13]) + int(fields[14])
                info["cpu_ticks"] = ticks
        except (FileNotFoundError, IndexError, ValueError):
            pass
        return info

    def _show_process_info(self, terminal, pid):
        info = self._get_process_info(pid)
        msg = (
            f"PID: {info['pid']}\n"
            f"Name: {info.get('name', '?')}\n"
            f"Command: {info.get('cmdline', '?')}\n"
            f"State: {info.get('state', '?')}\n"
            f"Memory: {info.get('rss', '?')}\n"
        )
        QMessageBox.information(terminal, "Process Info", msg)

    def _send_signal(self, terminal, pid, sig, action_name):
        """Send signal to the process (and its process group when possible).

        Foreground processes in a PTY often have their own process group
        (created by the shell's job control). Sending the signal to the
        process group ensures child processes are also signalled, which
        matches what Ctrl-C / Ctrl-Z do on the tty.
        """
        try:
            name = self._get_process_name(pid)
            sent = False
            try:
                pgid = os.getpgid(pid)
                # Only send to the process group if the target has its own
                # process group (typical for shell foreground jobs). If the
                # target shares our group — e.g. a subprocess spawned by the
                # test runner or the GUI itself — killpg would signal us too.
                if pgid != os.getpgrp() and pgid == pid:
                    os.killpg(pgid, sig)
                    sent = True
            except (ProcessLookupError, PermissionError, OSError):
                pass
            if not sent:
                os.kill(pid, sig)
            terminal._titlebar.set_title(f"{action_name}: {name}")
        except ProcessLookupError:
            QMessageBox.warning(terminal, "Signal Failed", f"Process {pid} not found.")
        except PermissionError:
            QMessageBox.warning(terminal, "Signal Failed", f"Permission denied for PID {pid}.")

    def _confirm_and_kill(self, terminal, pid):
        name = self._get_process_name(pid)
        result = QMessageBox.question(
            terminal, "Kill Process",
            f"Send SIGKILL to {name} (PID {pid})?\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if result == QMessageBox.StandardButton.Yes:
            self._send_signal(terminal, pid, signal.SIGKILL, "Killed")

    def _resume_group(self, terminal):
        """Send SIGCONT to the shell's process group."""
        shell_pid = terminal.shell_pid()
        if shell_pid > 0:
            try:
                os.killpg(os.getpgid(shell_pid), signal.SIGCONT)
            except (ProcessLookupError, PermissionError, OSError):
                pass
