"""Remote copy plugin for QFileMan.

Adds *Send via SCP / SFTP / FTP* entries to the context menu. The user
is prompted for a destination of the form ``user@host:/remote/path``
(``host:/path`` is also accepted; user is read from ``$LOGNAME`` /
``$USER`` by the underlying tool). FTP destinations are accepted in the
form ``ftp://[user@]host/path`` and copied with ``lftp``.

This plugin does not hold credentials. SSH-based transfers rely on
``ssh-agent`` or interactive prompts in a terminal; if you need a
password-prompt GUI, configure ``SSH_ASKPASS``. FTP passwords are not
accepted in URLs because they would be visible in process argv and the
command dialog; use lftp's normal interactive/auth config mechanisms
instead.

Command builders are pure functions so the dispatch logic is testable
without spawning any process.
"""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import urlsplit

from qfileman.plugin import MenuProvider

log = logging.getLogger(__name__)


# user@host:/path  or  host:/path  — minimal, doesn't try to be RFC strict.
SCP_DEST_RE = re.compile(r"^(?:[^@\s:/]+@)?[^\s:/]+:.*$")
FTP_DEST_RE = re.compile(r"^ftps?://", re.IGNORECASE)


def is_scp_dest(dest: str) -> bool:
    return bool(SCP_DEST_RE.match(dest)) and not FTP_DEST_RE.match(dest)


def is_ftp_dest(dest: str) -> bool:
    return bool(FTP_DEST_RE.match(dest))


def _local_dash_safe(path: str) -> str:
    """Prefix a leading-dash local path with ``./`` so scp reads it as a path,
    not an option. scp has no ``--`` option terminator, so this is the standard
    mitigation.

    This is applied to the *source*, which in this plugin is always the locally
    selected file/dir — never a remote spec. We therefore guard on the leading
    dash unconditionally: an ``is_scp_dest``-style colon check must NOT exempt
    it, since a hostile local filename can contain a ``:`` (e.g.
    ``-oProxyCommand=sh:foo``) and would otherwise slip through as an scp
    option → command execution.
    """
    if path.startswith("-"):
        return os.path.join(".", path)
    return path


def scp_argv(source: str, dest: str) -> list[str]:
    """``scp -rp source dest``. ``-r`` is harmless for files and required for dirs.

    The source is the locally selected file/dir; a leading-dash name is made
    scp-safe via ``./`` since scp offers no ``--`` terminator.
    """
    return ["scp", "-rp", _local_dash_safe(source), dest]


def sftp_batch_argv(source: str, dest: str) -> tuple[list[str], str] | None:
    """Build (argv, batch_script) for an sftp upload.

    ``dest`` must be in ``[user@]host:/remote/path`` form. The remote
    path is the *target* path of the upload; if it names a directory
    the file is dropped inside it, otherwise it becomes the file's new
    name (standard sftp ``put`` semantics).
    """
    if not is_scp_dest(dest):
        return None
    host_part, _, remote_path = dest.partition(":")
    if not remote_path:
        remote_path = "."
    # ``put -P`` preserves mtime+permissions; ``-r`` handles directories.
    script = f"put -rP {_sftp_quote(source)} {_sftp_quote(remote_path)}\n"
    return ["sftp", "-b", "-", host_part], script


def _sftp_quote(s: str) -> str:
    # sftp's batch parser is whitespace-split with double-quote support.
    return '"' + s.replace('"', '\\"') + '"'


def lftp_argv(source: str, dest: str) -> list[str] | None:
    """Build an ``lftp -e ...`` command to upload ``source`` to an FTP URL.

    ``dest`` must start with ``ftp://`` or ``ftps://`` and may include a
    user and remote path component:
    ``ftp://user@host/dir/`` — the trailing slash means "into this
    directory"; without it, ``dest`` is treated as the target filename.
    """
    if not is_ftp_dest(dest):
        return None
    parsed = urlsplit(dest)
    if parsed.password is not None:
        return None
    # Strip the scheme + creds to derive a base URL and a remote target.
    m = re.match(r"^(ftps?)://([^/]+)(/.*)?$", dest, re.IGNORECASE)
    if not m:
        return None
    scheme, authority, remote = m.group(1), m.group(2), m.group(3) or "/"
    if "@" in authority:
        userinfo, _, host = authority.rpartition("@")
        if ":" in userinfo:
            return None
        authority = f"{userinfo}@{host}"
    base = f"{scheme}://{authority}"
    if remote.endswith("/"):
        remote_dir = remote
        remote_name = os.path.basename(source.rstrip(os.sep))
    else:
        remote_dir, _, remote_name = remote.rpartition("/")
        remote_dir = remote_dir + "/"
    # ``mirror -R`` handles directories; ``put`` handles files. We pick
    # at call time based on whether ``source`` is a directory.
    if os.path.isdir(source):
        verb = f"mirror -R {_lftp_quote(source)} {_lftp_quote(remote_dir + remote_name)}"
    else:
        verb = (
            f"cd {_lftp_quote(remote_dir)}; "
            f"put -O . {_lftp_quote(source)} -o {_lftp_quote(remote_name)}"
        )
    script = f"{verb}; bye"
    return ["lftp", "-e", script, base]


def _lftp_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


class RemoteCopyPlugin(MenuProvider):
    name = "remote_copy"
    description = "Copy files to remote hosts via SCP, SFTP, or FTP"
    version = "1.0"
    category = "Tools"

    def get_menu_items(self, path):
        if not path:
            return []
        return [
            ("Send via SCP...", self._send_scp),
            ("Send via SFTP...", self._send_sftp),
            ("Send via FTP (lftp)...", self._send_ftp),
        ]

    def _send_scp(self, path: str) -> None:
        dest = self._prompt(
            "Send via SCP",
            "Destination (user@host:/path):",
            "user@host:" + (os.path.basename(path) or ""),
        )
        if not dest:
            return
        self._run("scp", "SCP " + os.path.basename(path), scp_argv(path, dest))

    def _send_sftp(self, path: str) -> None:
        dest = self._prompt(
            "Send via SFTP",
            "Destination (user@host:/path):",
            "user@host:/" + (os.path.basename(path) or ""),
        )
        if not dest:
            return
        built = sftp_batch_argv(path, dest)
        if built is None:
            self._warn("Destination must be in user@host:/path form.")
            return
        argv, script = built
        self._run("sftp", "SFTP " + os.path.basename(path), argv, stdin=script)

    def _send_ftp(self, path: str) -> None:
        dest = self._prompt(
            "Send via FTP",
            "Destination (ftp://[user@]host/dir/):",
            "ftp://host/",
        )
        if not dest:
            return
        argv = lftp_argv(path, dest)
        if argv is None:
            self._warn(
                "Destination must be an ftp:// or ftps:// URL without a password."
            )
            return
        self._run("lftp", "FTP " + os.path.basename(path), argv)

    # ------------------------------------------------------------- plumbing
    def _run(self, tool: str, title: str, argv: list[str], stdin: str | None = None) -> None:
        from qfileman.plugins.builtin._runner import (
            CommandDialog,
            missing_tools,
        )
        missing = missing_tools([tool])
        if missing:
            self._warn(f"Required tool not found on PATH: {missing[0]}")
            return
        # We piggyback on CommandDialog and optionally feed it stdin for sftp.
        dlg = CommandDialog(title, argv)
        if stdin is not None:
            dlg._process.write(stdin.encode("utf-8"))
            dlg._process.closeWriteChannel()
        dlg.exec()

    @staticmethod
    def _prompt(title: str, label: str, default: str) -> str | None:
        from PyQt6.QtWidgets import QInputDialog
        text, ok = QInputDialog.getText(None, title, label, text=default)
        return text.strip() if ok and text.strip() else None

    @staticmethod
    def _warn(message: str) -> None:
        from PyQt6.QtWidgets import QMessageBox
        log.warning(message)
        QMessageBox.warning(None, "Remote Copy", message)
