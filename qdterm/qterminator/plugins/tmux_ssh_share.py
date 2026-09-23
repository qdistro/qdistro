"""Share tmux sessions through a local OpenSSH server process."""

from __future__ import annotations

import getpass
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass

from PyQt6.QtWidgets import QMessageBox

from qterminator.config import CONFIG_DIR, Config
from qterminator.plugin import MenuProvider


def _free_tcp_port(bind: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((bind, 0))
        return int(sock.getsockname()[1])


@dataclass
class SSHShare:
    session: str
    bind: str
    port: int
    proc: subprocess.Popen
    temp_dir: str
    authorized_keys: str

    @property
    def connect_string(self) -> str:
        host = "127.0.0.1" if self.bind in ("0.0.0.0", "::") else self.bind
        return f"ssh -p {self.port} {getpass.getuser()}@{host}"

    def is_alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.temp_dir, ignore_errors=True)


class TmuxSSHShareService:
    def __init__(self, bind: str = "127.0.0.1",
                 authorized_keys: str | None = None):
        self.bind = bind
        self.authorized_keys = os.path.expanduser(
            authorized_keys
            or os.path.join(CONFIG_DIR, "authorized_keys")
        )
        self._shares: dict[str, list[SSHShare]] = {}

    def available(self) -> bool:
        return (
            shutil.which("sshd") is not None
            and shutil.which("ssh-keygen") is not None
            and shutil.which("tmux") is not None
        )

    def shares_for(self, session: str) -> list[SSHShare]:
        live = [s for s in self._shares.get(session, []) if s.is_alive()]
        if live:
            self._shares[session] = live
        else:
            self._shares.pop(session, None)
        return live

    def ports_for(self, session: str) -> list[int]:
        return [s.port for s in self.shares_for(session)]

    # NB: \Z (not $) -- $ matches just before a trailing newline, so the $
    # form would accept "attack\n", which then keys _shares and is fed into
    # the sshd ForceCommand. \Z anchors at the true end of string.
    _SAFE_SESSION = re.compile(r"^[a-zA-Z0-9_.\-]+\Z")

    def start_share(self, session: str, port: int = 0) -> SSHShare:
        if not self.available():
            raise RuntimeError("sshd, ssh-keygen, tmux are required for SSH sharing")
        if not self._SAFE_SESSION.match(session):
            raise ValueError(
                f"tmux session name {session!r} contains unsafe characters"
            )
        os.makedirs(os.path.dirname(self.authorized_keys), exist_ok=True)
        if not os.path.exists(self.authorized_keys):
            open(self.authorized_keys, "a").close()
            os.chmod(self.authorized_keys, 0o600)
        port = port or _free_tcp_port(self.bind)
        tmp = tempfile.mkdtemp(prefix="qterminator-sshd-")
        host_key = os.path.join(tmp, "ssh_host_ed25519_key")
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", host_key],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        config = os.path.join(tmp, "sshd_config")
        force = f"tmux attach -t {session}"
        with open(config, "w", encoding="utf-8") as f:
            f.write(
                "\n".join([
                    f"Port {port}",
                    f"ListenAddress {self.bind}",
                    f"HostKey {host_key}",
                    f"PidFile {os.path.join(tmp, 'sshd.pid')}",
                    "PasswordAuthentication no",
                    "KbdInteractiveAuthentication no",
                    "PubkeyAuthentication yes",
                    f"AuthorizedKeysFile {self.authorized_keys}",
                    "PermitTTY yes",
                    "AllowTcpForwarding no",
                    "X11Forwarding no",
                    "UsePAM no",
                    "LogLevel ERROR",
                    f"ForceCommand {force}",
                    "",
                ])
            )
        sshd = shutil.which("sshd") or "sshd"
        proc = subprocess.Popen(
            [sshd, "-D", "-e", "-f", config],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.15)
        if proc.poll() is not None:
            err = proc.stderr.read() if proc.stderr else ""
            shutil.rmtree(tmp, ignore_errors=True)
            raise RuntimeError(f"sshd failed to start: {err.strip()}")
        share = SSHShare(
            session=session,
            bind=self.bind,
            port=port,
            proc=proc,
            temp_dir=tmp,
            authorized_keys=self.authorized_keys,
        )
        self._shares.setdefault(session, []).append(share)
        return share

    def stop_all_for(self, session: str):
        for share in self._shares.get(session, []):
            share.stop()
        self._shares.pop(session, None)

    def stop_all(self):
        for session in list(self._shares):
            self.stop_all_for(session)


class TmuxSSHSharePlugin(MenuProvider):
    name = "tmux_ssh_share"
    description = "Share tmux sessions through a local SSH server"
    version = "0.1"
    category = "Workspace"
    capabilities = ["menu_provider", "tmux_ssh_share"]

    def __init__(self):
        super().__init__()
        self._window = None
        self._service: TmuxSSHShareService | None = None

    def activate(self, app_controller):
        self._window = app_controller
        cfg = Config()
        bind = cfg.get("plugins", "tmux_ssh_share", "bind", default="127.0.0.1")
        keys = cfg.get(
            "plugins", "tmux_ssh_share", "authorized_keys", default="",
        )
        self._service = TmuxSSHShareService(bind=bind, authorized_keys=keys or None)
        if not hasattr(app_controller, "tmux_ssh_share"):
            app_controller.tmux_ssh_share = self._service

    def deactivate(self):
        if self._service is not None:
            self._service.stop_all()
        if (self._window is not None
                and getattr(self._window, "tmux_ssh_share", None) is self._service):
            del self._window.tmux_ssh_share
        self._service = None
        self._window = None

    def get_menu_items(self, terminal):
        if self._service is None or not self._service.available():
            return []
        tmux_mode = getattr(self._window, "tmux_mode", None)
        if tmux_mode is None:
            return []
        session = tmux_mode.get_session_for_terminal(terminal)
        if not session:
            return []
        items = [(f"Share via SSH... ({session})", lambda s=session: self._start(s))]
        ports = self._service.ports_for(session)
        if ports:
            items.append((
                f"  Stop SSH sharing ({len(ports)} active)",
                lambda s=session: self._service.stop_all_for(s),
            ))
        return items

    def _start(self, session: str):
        try:
            share = self._service.start_share(session)
        except Exception as e:
            QMessageBox.critical(self._window, "SSH share failed", str(e))
            return
        QMessageBox.information(
            self._window,
            "SSH share started",
            f"{share.connect_string}\n\n"
            f"Authorized keys: {share.authorized_keys}",
        )
