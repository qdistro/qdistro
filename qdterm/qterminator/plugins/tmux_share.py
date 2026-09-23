"""Tmux session sharing via mosh.

Companion to ``tmux_mode``. When the current tab is backed by a tmux
session, the user can "Share via mosh…" — the plugin spawns
``mosh-server new -- tmux attach -t <session>``, captures the
``MOSH CONNECT <port> <key>`` line, and presents a dialog with the
``mosh-client`` invocation that a remote machine can paste into its
shell to attach.

Architecture:

  Local QTerminator tab  ──┐
                            ├── tmux session (qterm-N, persistent)
  Remote mosh-client ───────┘    via mosh-server new --
                                      tmux attach -t qterm-N

Multiple remote clients can share the same session — each
``mosh-server new`` is independent but they all wind up attached to
the same tmux. Closing the QTerminator tab does not disturb the
remote clients (the tmux server stays up, and they're not connected
through us).

Security defaults: binds ``mosh-server`` to ``127.0.0.1`` so the
share is only reachable to other processes on the same host (loopback
or via SSH port-forward). A config toggle allows a public interface
for direct LAN access — the dialog warns prominently when that path
is on.

Configuration (config.toml):

    [plugins.tmux_share]
    enabled = true              # default false
    bind = "127.0.0.1"          # "0.0.0.0" for LAN
    udp_port_range = "60000:61000"
"""

import os
import re
import shutil
import socket
import subprocess
from functools import partial

from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from qterminator.config import Config
from qterminator.plugin import MenuProvider

_MOSH_CONNECT_RE = re.compile(rb"MOSH CONNECT\s+(\d+)\s+(\S+)")
_MOSH_DETACHED_RE = re.compile(rb"mosh-server detached, pid\s*=\s*(\d+)")


def _mosh_available() -> bool:
    return shutil.which("mosh-server") is not None


def _local_ip() -> str:
    """Best-effort non-loopback local IP for display. The plugin's
    actual bind is controlled by config; this is just shown to the
    user so they know which address to mosh to."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        # Doesn't actually send anything; just resolves a route.
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


class Share:
    """A live mosh-server share. Fields are filled in by start()."""

    def __init__(self, session: str, bind: str):
        self.session = session
        self.bind = bind
        self.port: int | None = None
        self.key: str | None = None
        self.server_pid: int | None = None  # detached pid printed by mosh-server

    @property
    def connect_string(self) -> str:
        host = self.bind if self.bind != "0.0.0.0" else _local_ip()
        if self.port is None or self.key is None:
            return "(pending)"
        return f"MOSH_KEY={self.key} mosh-client {host} {self.port}"

    def is_alive(self) -> bool:
        if not self.server_pid:
            return False
        try:
            os.kill(self.server_pid, 0)
            return True
        except OSError:
            return False

    def kill(self):
        if self.server_pid:
            try:
                os.kill(self.server_pid, 15)  # SIGTERM
            except OSError:
                pass


def _read_proc_cmdline(pid: int) -> list[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return []
    return [p.decode("utf-8", errors="replace") for p in raw.split(b"\0") if p]


def _scan_mosh_server_pid(session: str, port: int | None = None) -> int | None:
    """Fallback when mosh-server omits the detached-pid banner."""
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        argv = _read_proc_cmdline(pid)
        if not argv:
            continue
        if "mosh-server" not in os.path.basename(argv[0]):
            continue
        joined = "\0".join(argv)
        if session not in joined:
            continue
        if port is not None and str(port) not in argv:
            # Some mosh-server builds keep only the requested range in argv;
            # treat the port check as a preference, not a hard failure.
            pass
        return pid
    return None


def _discover_running_shares(bind: str) -> dict[str, list[Share]]:
    """Best-effort startup scan for daemonized mosh-server processes."""
    shares: dict[str, list[Share]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return shares
    for entry in entries:
        if not entry.isdigit():
            continue
        argv = _read_proc_cmdline(int(entry))
        if not argv or "mosh-server" not in os.path.basename(argv[0]):
            continue
        session = None
        for i, arg in enumerate(argv[:-2]):
            if arg == "attach" and argv[i + 1] == "-t":
                session = argv[i + 2]
                break
        if not session:
            continue
        share = Share(session=session, bind=bind)
        share.server_pid = int(entry)
        for i, arg in enumerate(argv[:-1]):
            if arg == "-p" and argv[i + 1].isdigit():
                share.port = int(argv[i + 1])
        shares.setdefault(session, []).append(share)
    return shares


class TmuxShareService:
    """Public service exposed via ``app_controller.tmux_share``."""

    def __init__(self, bind: str = "127.0.0.1", port_range: str = "60000:61000"):
        self.bind = bind
        self.port_range = port_range
        # session_name -> list[Share]
        self._shares: dict[str, list[Share]] = {}

    def available(self) -> bool:
        return _mosh_available()

    def share_session(self, session: str,
                      tmux_socket: str | None = None) -> Share:
        """Spawn a mosh-server attached to the named tmux session.

        ``tmux_socket`` is the ``-L`` socket name (optional). Returns
        a populated :class:`Share` or raises ``RuntimeError`` if the
        mosh-server output couldn't be parsed.
        """
        if not _mosh_available():
            raise RuntimeError("mosh-server is not installed")

        argv = [
            "mosh-server", "new",
            "-i", self.bind,
            "-p", self.port_range,
        ]
        attach_cmd = ["tmux"]
        if tmux_socket:
            attach_cmd += ["-L", tmux_socket]
        attach_cmd += ["attach", "-t", session]
        argv += ["--", *attach_cmd]

        # mosh-server prints both port+key and the "[detached, pid=N]"
        # line on stdout, then daemonizes. Capture stdout from the
        # short-lived parent.
        try:
            proc = subprocess.run(
                argv, capture_output=True, timeout=5,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"mosh-server timed out: {e}") from e

        out = proc.stdout + proc.stderr
        m = _MOSH_CONNECT_RE.search(out)
        if not m:
            raise RuntimeError(
                f"mosh-server did not print MOSH CONNECT line: "
                f"{out!r}"
            )
        share = Share(session=session, bind=self.bind)
        share.port = int(m.group(1))
        share.key = m.group(2).decode("ascii")
        dm = _MOSH_DETACHED_RE.search(out)
        if dm:
            share.server_pid = int(dm.group(1))
        else:
            share.server_pid = _scan_mosh_server_pid(session, share.port)
        self._shares.setdefault(session, []).append(share)
        return share

    def restore_running(self):
        for session, shares in _discover_running_shares(self.bind).items():
            existing = {
                s.server_pid for s in self._shares.get(session, [])
                if s.server_pid is not None
            }
            for share in shares:
                if share.server_pid not in existing:
                    self._shares.setdefault(session, []).append(share)

    def shares_for(self, session: str) -> list[Share]:
        """Active shares for a session. Prunes dead entries on access."""
        live = [s for s in self._shares.get(session, []) if s.is_alive()]
        if live:
            self._shares[session] = live
        else:
            self._shares.pop(session, None)
        return live

    def ports_for(self, session: str) -> list[int]:
        return [s.port for s in self.shares_for(session) if s.port]

    def kill_all_for(self, session: str):
        for s in self._shares.get(session, []):
            s.kill()
        self._shares.pop(session, None)

    def kill_all(self):
        for sess in list(self._shares):
            self.kill_all_for(sess)


class _ShareDialog(QDialog):
    def __init__(self, share: Share, public_bind: bool, show_qr: bool = False,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Share Tmux Session via Mosh")
        self._share = share
        layout = QVBoxLayout(self)

        if public_bind:
            warn = QLabel(
                "<b style='color:#e74c3c'>⚠ Bound to a non-loopback "
                "interface.</b> Anyone on this network with the key "
                "below can attach. Treat the key as a one-time "
                "password — share it only out-of-band."
            )
            warn.setWordWrap(True)
            layout.addWidget(warn)

        layout.addWidget(QLabel(f"<b>Session:</b> {share.session}"))
        layout.addWidget(QLabel(f"<b>UDP port:</b> {share.port}"))
        layout.addWidget(QLabel("<b>Paste this on the remote machine:</b>"))

        self._line = QPlainTextEdit(share.connect_string)
        self._line.setReadOnly(True)
        self._line.setMaximumHeight(60)
        layout.addWidget(self._line)

        if show_qr:
            pixmap = self._qr_pixmap(share.connect_string)
            if pixmap is not None:
                qr = QLabel()
                qr.setPixmap(pixmap)
                qr.setToolTip("Connection QR code")
                layout.addWidget(qr)

        buttons = QHBoxLayout()
        copy_btn = QPushButton("Copy")
        copy_btn.clicked.connect(self._copy)
        buttons.addWidget(copy_btn)

        kill_btn = QPushButton("Stop sharing (kill mosh-server)")
        kill_btn.clicked.connect(self._kill)
        buttons.addWidget(kill_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        footer = QLabel(
            "Note: mosh-server self-terminates after ~60 seconds if no "
            "client connects, and on idle thereafter. If the remote "
            "side reports a timeout, click 'Share via mosh…' again to "
            "spawn a fresh server."
        )
        footer.setWordWrap(True)
        layout.addWidget(footer)

    @staticmethod
    def _qr_pixmap(text: str) -> QPixmap | None:
        try:
            import qrcode
        except ImportError:
            return None
        try:
            from io import BytesIO
            img = qrcode.make(text)
            buf = BytesIO()
            img.save(buf, format="PNG")
            pixmap = QPixmap()
            if pixmap.loadFromData(buf.getvalue(), "PNG"):
                return pixmap.scaledToWidth(180)
        except Exception:
            return None
        return None

    def _copy(self):
        QApplication.clipboard().setText(self._share.connect_string)

    def _kill(self):
        self._share.kill()
        QMessageBox.information(
            self, "Stopped", f"mosh-server (pid {self._share.server_pid}) "
            "signalled to stop.",
        )
        self.accept()


class TmuxSharePlugin(MenuProvider):
    name = "tmux_share"
    description = "Share local tmux sessions over mosh"
    version = "0.1"
    category = "Workspace"
    capabilities = ["menu_provider", "tmux_share"]

    def __init__(self):
        super().__init__()
        self._window = None
        self._service: TmuxShareService | None = None
        self._enabled = False
        self._show_qr = False

    def activate(self, app_controller):
        self._window = app_controller
        cfg = Config()
        self._enabled = bool(cfg.get(
            "plugins", "tmux_share", "enabled", default=False,
        ))
        bind = cfg.get("plugins", "tmux_share", "bind", default="127.0.0.1")
        port_range = cfg.get(
            "plugins", "tmux_share", "udp_port_range",
            default="60000:61000",
        )
        self._service = TmuxShareService(bind=bind, port_range=port_range)
        self._show_qr = bool(cfg.get(
            "plugins", "tmux_share", "show_qr", default=False,
        ))
        # Expose for agent_control / other plugins regardless of enabled
        # state — they may want to introspect existing shares.
        if not hasattr(app_controller, "tmux_share"):
            app_controller.tmux_share = self._service
        self._service.restore_running()
        self._update_titlebar_indicators()

    def deactivate(self):
        if self._service:
            self._service.kill_all()
        if self._window is not None and \
                getattr(self._window, "tmux_share", None) is self._service:
            try:
                del self._window.tmux_share
            except AttributeError:
                pass
        self._service = None
        self._clear_titlebar_indicators()

    # -- menu --

    def get_menu_items(self, terminal):
        if not self._service or not self._service.available():
            return []
        tmux_mode = getattr(self._window, "tmux_mode", None)
        if tmux_mode is None:
            return []
        sess = tmux_mode.get_session_for_terminal(terminal)
        if not sess:
            return []
        items = [
            (f"Share via Mosh… ({sess})",
             lambda s=sess: self._share_and_show(s)),
        ]
        ports = self._service.ports_for(sess)
        if ports:
            items.append((
                f"  Stop sharing ({len(ports)} active)",
                lambda s=sess: self._stop_sharing(s),
            ))
        return items

    def _stop_sharing(self, session: str):
        if self._service is not None:
            self._service.kill_all_for(session)
        self._update_titlebar_indicators()

    def _share_and_show(self, session: str):
        try:
            share = self._service.share_session(session)
        except RuntimeError as e:
            QMessageBox.critical(self._window, "Mosh share failed", str(e))
            return
        dlg = _ShareDialog(
            share,
            public_bind=(self._service.bind not in ("127.0.0.1", "::1", "localhost")),
            show_qr=self._show_qr,
            parent=self._window,
        )
        self._update_titlebar_indicators()
        dlg.exec()
        self._update_titlebar_indicators()

    def _terminals_for_session(self, session: str):
        tmux_mode = getattr(self._window, "tmux_mode", None)
        tabs = getattr(self._window, "_tabs", None)
        if tmux_mode is None or tabs is None:
            return []
        out = []
        for i in range(tabs.count()):
            split = tabs.widget(i)
            for terminal in split.find_terminals():
                try:
                    if tmux_mode.get_session_for_terminal(terminal) == session:
                        out.append(terminal)
                except Exception:
                    pass
        return out

    def _update_titlebar_indicators(self):
        if self._service is None:
            return
        sessions = list(self._service._shares)
        for session in sessions:
            count = len(self._service.shares_for(session))
            for terminal in self._terminals_for_session(session):
                self._set_titlebar_indicator(terminal, session, count)

    def _clear_titlebar_indicators(self):
        tabs = getattr(self._window, "_tabs", None) if self._window else None
        if tabs is None:
            return
        for i in range(tabs.count()):
            split = tabs.widget(i)
            for terminal in split.find_terminals():
                label = getattr(terminal._titlebar, "_tmux_share_label", None)
                if label is not None:
                    label.hide()

    def _set_titlebar_indicator(self, terminal, session: str, count: int):
        titlebar = getattr(terminal, "_titlebar", None)
        if titlebar is None:
            return
        label = getattr(titlebar, "_tmux_share_label", None)
        if label is None:
            label = QLabel(titlebar)
            label.setStyleSheet("color: #8fd19e; font-size: 10px; font-weight: bold;")
            titlebar.layout().insertWidget(3, label)
            titlebar._tmux_share_label = label
        if count <= 0:
            label.hide()
            return
        shares = self._service.shares_for(session) if self._service else []
        ports = ", ".join(str(s.port or "?") for s in shares)
        label.setText(f"M{count}")
        label.setToolTip(f"Active mosh shares for {session}: {ports}")
        label.mousePressEvent = partial(self._show_share_manager, session)
        label.show()

    def _show_share_manager(self, session: str, _event=None):
        if self._service is None:
            return
        shares = self._service.shares_for(session)
        if not shares:
            QMessageBox.information(self._window, "Tmux shares", "No active shares.")
            self._update_titlebar_indicators()
            return
        text = "\n".join(
            f"pid={s.server_pid or '?'} port={s.port or '?'} {s.connect_string}"
            for s in shares
        )
        msg = QMessageBox(self._window)
        msg.setWindowTitle("Active tmux shares")
        msg.setText(f"Session: {session}")
        msg.setInformativeText(text)
        stop = msg.addButton("Stop all", QMessageBox.ButtonRole.DestructiveRole)
        msg.addButton(QMessageBox.StandardButton.Close)
        msg.exec()
        if msg.clickedButton() is stop:
            self._service.kill_all_for(session)
            self._update_titlebar_indicators()
