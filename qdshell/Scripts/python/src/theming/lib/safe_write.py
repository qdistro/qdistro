"""Confined, symlink-safe output writing for the theming pipeline (F3).

Template ``output_path`` values come from TOML config (``[templates.*]``), the
``--render input:output`` CLI map, and the terminal-output map. Without
confinement an ``output_path`` could escape into shell rc files, autostart /
systemd user units, ssh keys, or anywhere in ``$HOME``; and a pre-planted symlink
at the output path could redirect the write to its target (TOCTOU). This module:

  * restricts writes to the config/cache/data trees the theming pipeline
    legitimately targets (``~/.config``, ``~/.cache``, ``~/.local/share``,
    ``~/.local/state``);
  * hard-denies persistence / credential subtrees even within those roots
    (autostart, systemd user units, environment.d, shell config dirs, ssh,
    gnupg, ``~/.local/bin``);
  * resolves the path first (collapsing any existing symlinked parent, so a
    parent symlink that escapes the allowed roots is rejected), then opens the
    final file with ``O_NOFOLLOW`` so a symlinked output target cannot redirect
    the write.
"""

from __future__ import annotations

import os
from pathlib import Path


class OutputConfinementError(Exception):
    """Raised when an output path is not an allowed theming target."""


def _home() -> Path:
    return Path(os.path.expanduser("~")).resolve()


def allowed_bases(home: Path | None = None) -> list[Path]:
    h = home or _home()
    return [
        h / ".config",
        h / ".cache",
        h / ".local" / "share",
        h / ".local" / "state",
        # Built-in TemplateRegistry.qml targets that live outside the XDG roots:
        # VSCode / VSCodium extension theme files and the Emacs client dir.
        h / ".vscode",
        h / ".vscode-oss",
        h / ".emacs.d",
    ]


def denied_subtrees(home: Path | None = None) -> list[Path]:
    h = home or _home()
    return [
        h / ".config" / "autostart",
        h / ".config" / "systemd",
        h / ".config" / "environment.d",
        h / ".config" / "plasma-workspace",
        h / ".config" / "fish",
        h / ".config" / "zsh",
        h / ".config" / "bash",
        h / ".config" / "profile.d",
        h / ".ssh",
        h / ".gnupg",
        h / ".local" / "bin",
    ]


def _is_within(path: Path, base: Path) -> bool:
    return path == base or base in path.parents


def confine_output_path(path: str | os.PathLike) -> Path:
    """Resolve ``path`` and assert it is an allowed theming output location.

    Returns the resolved :class:`~pathlib.Path` or raises
    :class:`OutputConfinementError`.
    """
    home = _home()
    p = Path(os.path.expanduser(str(path)))
    p = p if p.is_absolute() else home / p
    # Resolve the PARENT only (collapsing ``..`` and any existing symlinked
    # parent to its real location) and keep the final component literal. This is
    # deliberate: if we resolved the whole path we would collapse a symlinked
    # *final target* too, and then open its real destination — defeating the
    # O_NOFOLLOW check in safe_write_text. The parent need not exist yet.
    resolved = p.parent.resolve(strict=False) / p.name

    if not any(_is_within(resolved, b) for b in allowed_bases(home)):
        raise OutputConfinementError(
            f"output path outside allowed theming roots: {resolved}"
        )
    for d in denied_subtrees(home):
        if _is_within(resolved, d):
            raise OutputConfinementError(
                f"output path in a denied subtree: {resolved}"
            )
    return resolved


def safe_write_text(path: str | os.PathLike, text: str, *, mode: int = 0o644) -> Path:
    """Confine ``path``, create parents within the allowed root, and write
    ``text`` with ``O_NOFOLLOW`` so a symlinked target cannot redirect the write.

    Returns the resolved path. Raises :class:`OutputConfinementError` if the path
    is not an allowed theming target; ``OSError`` (``ELOOP``) if the final path is
    a symlink.
    """
    resolved = confine_output_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    fd = os.open(str(resolved), flags, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return resolved
