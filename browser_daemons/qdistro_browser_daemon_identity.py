"""Layered caller identity for the Phase-9e browser desktop daemons.

The four 9e daemons (MPRIS / Downloads / Notifications / Compositor)
all receive calls from the browser bridge over the SESSION bus. They
share one auth gate, factored out here so the policy is written once
and the daemons stay thin.

Trust model (same anchors as ``qdistro_pwd_identity`` /
``qdistro_admin_broker``): the daemon NEVER trusts a caller-supplied
identity claim. Every security input is read from kernel-attested
sources —

  * uid / pid from D-Bus ``GetConnectionUnixUser`` /
    ``GetConnectionUnixProcessID`` (the bus daemon's SO_PEERCRED view of
    the peer), and
  * the *executed script* + its parent-browser exe from ``/proc/<pid>``.

The browser bridge is installed as
``python3 /usr/libexec/qdistro/qdistro_browser_bridge.py`` and is
spawned by an allowlisted RPM browser binary. We require both facts —
mirroring ``qdistro_pwd_daemon._browser_bridge_allowed`` — so a random
same-uid Python process cannot publish media / fake download
notifications by talking to these daemons directly.

The JSON body the bridge forwards carries advisory ``extension_id`` /
``parent_exe`` fields (the bridge's *own* view), used only for audit and
for the MPRIS player-name suffix; the authoritative parent-browser exe
for the security decision is re-read here from ``/proc``.
"""
from __future__ import annotations

import os
from typing import Any

# Default browser bridge script path. Overridable for tests / non-RPM
# layouts via the env var, matching the pwd daemon's
# QDISTRO_PWD_BROWSER_BRIDGE_SCRIPT escape hatch.
BROWSER_BRIDGE_SCRIPT = os.environ.get(
    "QDISTRO_BROWSER_BRIDGE_SCRIPT",
    "/usr/libexec/qdistro/qdistro_browser_bridge.py")

# Allowlisted parent-browser exes. Same default matrix as the bridge's
# ALLOWED_PARENT_EXES (qdistro_browser_bridge.py) and the pwd daemon's
# BROWSER_PARENT_EXES. Overridable for tests.
BROWSER_PARENT_EXES = tuple(
    p for p in os.environ.get(
        "QDISTRO_BROWSER_PARENT_EXES",
        ":".join((
            "/usr/lib64/firefox/firefox",
            "/usr/lib/firefox/firefox",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/brave",
            "/usr/bin/brave-browser",
            "/usr/bin/vivaldi",
            "/usr/bin/vivaldi-stable",
            "/usr/bin/microsoft-edge",
            "/usr/bin/microsoft-edge-stable",
        ))).split(":") if p)

# Valueless interpreter flags tolerated before the executed script. A
# flag that consumes the following token as an operand (-c/-m/-W/-X …)
# could smuggle the real bridge path into an option value while Python
# runs a different file; anything not in this set fails closed. Copied
# verbatim from qdistro_pwd_daemon._browser_bridge_allowed so the two
# gates can't drift.
_VALUELESS_FLAGS = frozenset({
    "-b", "-bb", "-B", "-d", "-E", "-i", "-I", "-O", "-OO",
    "-q", "-s", "-S", "-u", "-v", "-vv", "-x",
})


def read_proc_cmdline(pid: int) -> list[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read(16384)
    except OSError:
        return []
    return [p.decode("utf-8", "replace") for p in raw.split(b"\x00") if p]


def read_proc_ppid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("PPid:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def read_proc_exe(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return ""


def browser_bridge_allowed(
        pid: int,
        *,
        bridge_script: str | None = None,
        parent_exes: tuple[str, ...] | None = None,
        cmdline_reader=read_proc_cmdline,
        ppid_reader=read_proc_ppid,
        exe_reader=read_proc_exe,
) -> tuple[bool, str]:
    """Verify the calling pid is the qdistro browser native-messaging host.

    Returns ``(allowed, reason)``. ``reason`` is a short tag used in the
    daemon's audit line and in the error reply. Fails closed on any
    unreadable /proc entry (a racing/exited caller).

    The readers are injectable so unit tests can drive every branch
    without a real /proc layout.
    """
    script_real = os.path.realpath(bridge_script or BROWSER_BRIDGE_SCRIPT)
    allowed_parents = {
        os.path.realpath(p)
        for p in (parent_exes if parent_exes is not None
                  else BROWSER_PARENT_EXES)
    }
    cmdline = cmdline_reader(pid)
    # Locate the executed script: skip argv[0] (interpreter) and any
    # leading valueless interpreter flags; the first non-flag token is
    # the script Python actually runs.
    executed_script = ""
    for arg in cmdline[1:]:
        if arg.startswith("-"):
            if arg in _VALUELESS_FLAGS:
                continue
            return False, "not-browser-bridge"
        executed_script = arg
        break
    if (not executed_script
            or os.path.realpath(executed_script) != script_real):
        return False, "not-browser-bridge"
    ppid = ppid_reader(pid)
    if ppid is None:
        return False, "parent-unreadable"
    parent_exe = exe_reader(ppid)
    if not parent_exe or os.path.realpath(parent_exe) not in allowed_parents:
        return False, "parent-not-browser"
    return True, "browser-bridge"


def username_for_uid(uid: int) -> str:
    """Resolve a uid to its login name, falling back to ``uid:<n>`` so
    the result is always a well-formed, addressable silo label even for
    a freshly-provisioned uid with no passwd entry."""
    import pwd as _pwd
    try:
        return _pwd.getpwuid(int(uid)).pw_name
    except (KeyError, ValueError):
        return f"uid:{int(uid)}"


def browser_label(parent_exe: str) -> str:
    """Map a parent-browser exe path to a short, D-Bus-name-safe label
    (``firefox`` / ``chromium`` / ``brave`` …) for the MPRIS player name.

    Returns ``unknown`` when the exe is empty / unrecognised. The result
    only ever contains ``[a-z0-9]`` so it is safe to interpolate into an
    ``org.mpris.MediaPlayer2.*`` well-known name segment without further
    escaping.
    """
    base = os.path.basename(parent_exe or "").lower()
    # Strip common suffixes so google-chrome-stable -> chrome, etc.
    known = (
        ("firefox", "firefox"),
        ("chromium", "chromium"),
        ("google-chrome", "chrome"),
        ("chrome", "chrome"),
        ("brave", "brave"),
        ("vivaldi", "vivaldi"),
        ("microsoft-edge", "edge"),
        ("edge", "edge"),
    )
    for needle, label in known:
        if needle in base:
            return label
    return "unknown"


def caller_advisory(req: dict[str, Any]) -> tuple[str, str]:
    """Extract advisory (extension_id, parent_exe) from a request body.

    Both are request-controlled metadata used ONLY for audit + the MPRIS
    player-name suffix — never a security input. Bounded length, control
    chars stripped, so a hostile bridge can't smuggle a blob into an
    audit row or a bus name.
    """
    def _clean(v: object, limit: int) -> str:
        if not isinstance(v, str) or not v:
            return ""
        return "".join(ch for ch in v if ch.isprintable())[:limit].strip()

    ext = _clean(req.get("extension_id"), 128)
    exe = _clean(req.get("parent_exe"), 256)
    return ext, exe
