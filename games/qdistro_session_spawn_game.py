"""qdistro-session-manager spawn-game — Phase-8 dry-run probe.

Per doc/games.md §"Phase-8 MVP scope". The spec calls for a
tiny probe that writes the ephemeral greetd config to a tmpdir
and asserts shape — no real chvt, no real launcher. This module
implements the config-rendering layer + the dry-run path. The
real launcher (chvt + systemd-run) is a Phase-9 deliverable.

Config target: ``/run/greetd/qdistro-game-<vt>.toml``. Picked
``/run`` because greetd's docs allow it and it's tmpfs (no
disk persistence between reboots).

Launcher choice from spec/12:

- ``cage`` — in-tree on Tumbleweed, smallest kiosk compositor.
  Default for Phase-8 since it's distro-packaged.
- ``gamescope`` — Valve's compositor; third-party Tumbleweed
  package via ``games:tools`` OBS branch. Better default for
  Steam/Proton workloads but operationally heavier.
"""
from __future__ import annotations

import os
import re
import shlex
from typing import Iterable

# Allowlist of compositors. Tests can extend; spec keeps this short.
ALLOWED_LAUNCHERS = ("cage", "gamescope")

# Allowlist for the launcher's binary path. Both packages install
# under /usr/bin on Tumbleweed.
LAUNCHER_BINARIES = {
    "cage": "/usr/bin/cage",
    "gamescope": "/usr/bin/gamescope",
}

# Shell-safe chars in argv elements.
_SAFE_ARGV = re.compile(r"^[A-Za-z0-9_./:=@,+\-]+$")


def _validate_user(user: str) -> str:
    if not user:
        raise ValueError("user is required")
    if not re.match(r"^[a-z][a-z0-9_-]{0,31}$", user):
        raise ValueError(
            f"user must match POSIX user-name shape: {user!r}")
    return user


def _validate_vt(vt: int) -> int:
    if not isinstance(vt, int) or vt < 4 or vt > 12:
        raise ValueError(
            f"vt must be 4..12 (admin owns tty3): {vt!r}")
    return vt


def _validate_launcher(launcher: str) -> str:
    if launcher not in ALLOWED_LAUNCHERS:
        raise ValueError(
            f"launcher must be one of {ALLOWED_LAUNCHERS}: "
            f"{launcher!r}")
    return launcher


def _quote_argv_element(s: str) -> str:
    """Quote a single argv element for inclusion in greetd's
    `command = "..."` field. Greetd parses the command with
    shell-style splitting.
    """
    s = str(s)
    if not s:
        return "''"
    if _SAFE_ARGV.match(s):
        return s
    return shlex.quote(s)


def render_command(
        launcher: str,
        argv: Iterable[str] | None = None,
) -> str:
    """Build the `command = "..."` value for the greetd config.

    For cage: ``cage -s -- <argv...>``  (-s sleeps until child
    exits cleanly; cage itself exits when its single client
    exits).
    For gamescope: ``gamescope -- <argv...>``.

    `argv` is the user's launcher binary + its own args (e.g.
    ``["steam"]`` or ``["/usr/bin/firefox", "--kiosk"]``).
    Empty argv is allowed in dry-run for shape testing — the real
    spawner refuses an empty argv with a separate error.
    """
    launcher = _validate_launcher(launcher)
    bin_path = LAUNCHER_BINARIES[launcher]
    argv = list(argv or [])
    parts = [bin_path]
    if launcher == "cage":
        parts.append("-s")
    parts.append("--")
    parts.extend(argv)
    return " ".join(_quote_argv_element(p) for p in parts)


def render_greetd_config(
        *,
        user: str,
        vt: int,
        launcher: str,
        argv: Iterable[str] | None = None,
) -> str:
    """Build the full greetd-config TOML body for the game session."""
    _validate_user(user)
    _validate_vt(vt)
    cmd = render_command(launcher, argv)
    body = (
        f"# qdistro-session-manager spawn-game (auto-generated)\n"
        f"# spec/12 §Phase-8 MVP — ephemeral greetd config.\n"
        f"# Removed when the session ends.\n"
        f"\n"
        f"[terminal]\n"
        f"vt = {vt}\n"
        f"switch = false\n"
        f"\n"
        f"[default_session]\n"
        f"user = {_quote_argv_element(user)}\n"
        f"command = \"{cmd}\"\n"
    )
    return body


def config_path(vt: int, root: str = "/run/greetd") -> str:
    """Path the spawner writes to. Tests override `root`."""
    _validate_vt(vt)
    return os.path.join(os.fspath(root), f"qdistro-game-{vt}.toml")


def write_config_dry_run(
        *,
        user: str,
        vt: int,
        launcher: str,
        argv: Iterable[str] | None = None,
        root: str | os.PathLike = "/run/greetd",
) -> tuple[str, str]:
    """Render + write the greetd config to a temp/test directory.

    Returns ``(path, body)``. Real chvt + systemd-run NOT performed
    — Phase-9 wires those. Caller (spec/12 phase8 bats) reads the
    body to assert shape.
    """
    path = config_path(vt, root=os.fspath(root))
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    body = render_greetd_config(
        user=user, vt=vt, launcher=launcher, argv=argv)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(path, 0o600)
    return path, body
