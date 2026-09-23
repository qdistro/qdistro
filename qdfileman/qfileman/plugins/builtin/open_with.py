"""Open-with plugin for QFileMan.

Adds *Open With…* — a picker showing the desktop applications that
declare support for the selected file's MIME type, plus a free-form
command field as a fallback. The pattern matches Dolphin's *Open With*
and Nautilus's *Open With Other Application*.

Process:

1. Determine the file's MIME type via ``xdg-mime query filetype``.
2. Ask ``xdg-mime query default <type>`` for the system default, plus
   parse ``$XDG_DATA_DIRS``/``$XDG_DATA_HOME`` ``mimeinfo.cache`` and
   ``mimeapps.list`` files to collect every registered handler.
3. For each handler ``foo.desktop`` we read ``Name`` and ``Exec`` from
   the file so the picker can show a friendly label.
4. Launch the chosen application with the file path substituted into
   the ``Exec`` line according to the Desktop Entry Spec field codes
   (``%f`` / ``%u``, etc.).

The plugin avoids any KDE/GNOME-specific machinery so it works on
plain X11 / Wayland as well as Plasma / GNOME sessions.

:func:`format_exec` is a pure function and exercised by tests; the
rest of the parsing is small enough to inline.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess

from qfileman.plugin import MenuProvider

log = logging.getLogger(__name__)


# Per the Desktop Entry Spec, Exec field codes:
#   %f  single filename
#   %F  list of files
#   %u  single URL
#   %U  list of URLs
#   %i  --icon flag (skip)
#   %c  translated Name (skip)
#   %k  desktop file location (skip)
#   %%  literal %
_FIELD_CODE_RE = re.compile(r"%[fFuUiIcCkdDnNvm%]")


def format_exec(exec_line: str, path: str) -> list[str]:
    """Expand a Desktop Entry ``Exec`` field for a single file.

    Returns an argv ready for :func:`subprocess.Popen`. We always use
    the single-file codes (``%f``/``%u``) — multi-file handlers see a
    one-element list, which is what they expect anyway.
    """
    # Quote the path so shlex round-trips it correctly even when it
    # contains shell metacharacters.
    quoted = shlex.quote(path)
    uri = "file://" + path  # naïve but matches what xdg-open does

    def _sub(m: re.Match) -> str:
        code = m.group(0)
        if code in ("%f", "%F"):
            return quoted
        if code in ("%u", "%U"):
            return shlex.quote(uri)
        if code == "%%":
            return "%"
        # Drop %i / %c / %k / deprecated codes silently.
        return ""

    expanded = _FIELD_CODE_RE.sub(_sub, exec_line).strip()
    # If the Exec line didn't reference the file at all, append it so
    # the chosen app still receives the target.
    try:
        argv = shlex.split(expanded)
    except ValueError:
        # Malformed quoting in the .desktop entry — fall back to the
        # literal line and let the user see the failure.
        argv = expanded.split()
    if path not in argv and not any(a.endswith(path) for a in argv):
        argv.append(path)
    return argv


def mime_for(path: str) -> str | None:
    """Return the MIME type of ``path`` via ``xdg-mime``, or ``None``."""
    if not shutil.which("xdg-mime"):
        return None
    try:
        out = subprocess.check_output(
            ["xdg-mime", "query", "filetype", path],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("xdg-mime query failed: %s", e)
        return None
    return out.strip() or None


def _data_dirs() -> list[str]:
    home = os.environ.get(
        "XDG_DATA_HOME",
        os.path.expanduser("~/.local/share"),
    )
    system = os.environ.get(
        "XDG_DATA_DIRS",
        "/usr/local/share:/usr/share",
    )
    return [home, *system.split(":")]


def _find_desktop_file(name: str) -> str | None:
    """Locate ``name`` (e.g. ``foo.desktop``) under XDG data dirs."""
    for base in _data_dirs():
        candidate = os.path.join(base, "applications", name)
        if os.path.isfile(candidate):
            return candidate
        # Some distros use subdirs: kde4-foo.desktop etc.
        sub = os.path.join(base, "applications", *name.split("-"))
        if os.path.isfile(sub):
            return sub
    return None


def _parse_desktop_entry(path: str) -> dict[str, str] | None:
    """Read a ``.desktop`` file's ``[Desktop Entry]`` section. None on error."""
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        log.debug("read %s: %s", path, e)
        return None
    in_section = False
    out: dict[str, str] = {}
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_section = line == "[Desktop Entry]"
            continue
        if not in_section or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def handlers_for(mime: str) -> list[tuple[str, str, str]]:
    """Return ``(name, desktop_id, exec)`` tuples for apps handling ``mime``.

    Reads ``mimeinfo.cache`` from every XDG data dir's
    ``applications/`` subdirectory — that's the index every conforming
    installer writes when a ``.desktop`` file declares a MimeType.
    """
    seen_ids: set[str] = set()
    out: list[tuple[str, str, str]] = []
    for base in _data_dirs():
        cache = os.path.join(base, "applications", "mimeinfo.cache")
        if not os.path.isfile(cache):
            continue
        try:
            with open(cache, encoding="utf-8") as f:
                content = f.read()
        except OSError:
            continue
        for raw in content.splitlines():
            if "=" not in raw or not raw.startswith(mime + "="):
                continue
            _key, _, rest = raw.partition("=")
            for desktop_id in rest.split(";"):
                desktop_id = desktop_id.strip()
                if not desktop_id or desktop_id in seen_ids:
                    continue
                seen_ids.add(desktop_id)
                desktop_path = _find_desktop_file(desktop_id)
                if not desktop_path:
                    continue
                entry = _parse_desktop_entry(desktop_path)
                if not entry or "Exec" not in entry:
                    continue
                if entry.get("NoDisplay", "false").lower() == "true":
                    continue
                out.append((
                    entry.get("Name", desktop_id),
                    desktop_id,
                    entry["Exec"],
                ))
    return out


class OpenWithPlugin(MenuProvider):
    name = "open_with"
    description = "Open a file with any application registered for its MIME type"
    version = "1.0"
    category = "File"

    def get_menu_items(self, path):
        if not path or not os.path.isfile(path):
            return []
        return [("Open With...", self._open_with)]

    def _open_with(self, path: str) -> None:
        from PyQt6.QtWidgets import QInputDialog

        mime = mime_for(path)
        handlers = handlers_for(mime) if mime else []
        labels = [f"{name} ({desktop_id})" for name, desktop_id, _exec in handlers]
        labels.append("Other command…")

        choice, ok = QInputDialog.getItem(
            None, "Open With", f"MIME: {mime or 'unknown'}",
            labels, 0, False,
        )
        if not ok:
            return

        if choice == "Other command…":
            cmd, ok = QInputDialog.getText(
                None, "Open With Command",
                "Command (use %f for the path):",
                text="",
            )
            if not (ok and cmd.strip()):
                return
            argv = format_exec(cmd, path)
        else:
            idx = labels.index(choice)
            _name, _id, exec_line = handlers[idx]
            argv = format_exec(exec_line, path)

        try:
            subprocess.Popen(argv)
        except OSError as e:
            self._warn(f"Launch failed: {e}")

    @staticmethod
    def _warn(message: str) -> None:
        from PyQt6.QtWidgets import QMessageBox
        log.warning(message)
        QMessageBox.warning(None, "Open With", message)
