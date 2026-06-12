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

# Shared parent-browser allowlist (P0-4 follow-up). The trusted parent set
# is resolved through the SAME module the bridge entry gate uses
# (``qdistro_browser_allowlist``) so this defense-in-depth gate cannot drift
# wider than the gate that already rejected an un-opted-in parent before any
# forward reached us. The optional browsers (Chrome/Brave/Vivaldi/Edge) are
# admitted only when an admin has opted them in via the root-owned config;
# the Firefox+Chromium baseline is always trusted.
#
# Both the bridge and the pwd daemon import this module; it installs
# alongside them under /usr/libexec/qdistro/. We import defensively (the
# same pattern the bridge uses for qdistro_proc_identity): if the module is
# somehow absent, fall back to the Firefox+Chromium BASELINE — the
# narrowest, fail-closed set — never the historical full matrix.
try:
    import qdistro_browser_allowlist as _allowlist  # type: ignore
except Exception:  # noqa: BLE001 — fail closed to the baseline if unavailable
    _allowlist = None  # type: ignore[assignment]

_BASELINE_PARENT_EXES: tuple[str, ...] = (
    "/usr/lib64/firefox/firefox",
    "/usr/lib/firefox/firefox",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)

# Optional full-override escape hatch for tests / non-RPM layouts. When
# ``QDISTRO_BROWSER_PARENT_EXES`` is set it REPLACES the resolved set
# entirely (the historical behaviour); when unset the effective set is the
# baseline + admin opt-in, read live at gate time so an opt-in config edit
# takes effect without restarting the daemon.
_PARENT_EXES_ENV_OVERRIDE: tuple[str, ...] | None = (
    tuple(p for p in os.environ["QDISTRO_BROWSER_PARENT_EXES"].split(":") if p)
    if os.environ.get("QDISTRO_BROWSER_PARENT_EXES") is not None
    else None)


def resolve_parent_exes() -> tuple[str, ...]:
    """Effective trusted parent-browser exes for the daemon identity gate.

    Resolution order: an explicit ``QDISTRO_BROWSER_PARENT_EXES`` override
    (full replacement) wins; otherwise the shared module's baseline +
    admin-opt-in resolution; otherwise (module unavailable) the
    Firefox+Chromium baseline, fail-closed.
    """
    if _PARENT_EXES_ENV_OVERRIDE is not None:
        return _PARENT_EXES_ENV_OVERRIDE
    if _allowlist is not None:
        return _allowlist.resolve_parent_exes()
    return _BASELINE_PARENT_EXES

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
                  else resolve_parent_exes())
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


# --------------------------------------------------------------------- #
# qdbrowser forward allowance (Track 02 unification — explicit, narrow)
# --------------------------------------------------------------------- #
#
# qdbrowser is a *first-party* qdistro browser, not a third-party browser
# launched by an allowlisted RPM binary, so it can never satisfy
# ``browser_bridge_allowed`` (no native-messaging host script, no
# allowlisted parent-browser exe). Yet its ``DaemonForwarder`` needs to
# reach the same SESSION-bus daemons so qdbrowser media/downloads surface
# in the admin widget alongside Firefox/Chrome.
#
# The allowance is a deliberate, NARROW security decision:
#
#   * It admits ONLY qdbrowser's real identity — a process whose
#     kernel-attested *executed script* (read from /proc, never the body)
#     is the installed ``qdbrowser`` console-script entry point. It is NOT
#     "any same-uid parent" and NOT a body-supplied marker (the
#     ``parent_exe: "qdbrowser"`` field in the forward body stays purely
#     advisory — used for the player-name label / audit, never trusted).
#   * The executed-script resolution reuses the exact valueless-flag /
#     operand-flag rules ``browser_bridge_allowed`` uses, so a hostile
#     ``python3 -W org/qdbrowser evil.py`` cannot smuggle the allowed path
#     into a flag operand while Python runs a different file.
#   * It fails closed on any unreadable /proc entry.
#
# Caller uid is still resolved by the daemon from SO_PEERCRED, so this
# only decides *whether* a qdbrowser caller may forward — never *which
# user* the forward belongs to (that is always the attested uid).
#
# The allowlist defaults to the common ``-e .`` / packaged console-script
# locations and is overridable for tests / non-standard layouts via
# ``QDISTRO_QDBROWSER_SCRIPTS`` (``:``-separated). Empty list ⇒ no
# qdbrowser caller is ever admitted (fail-closed by configuration).

QDBROWSER_SCRIPTS = tuple(
    p for p in os.environ.get(
        "QDISTRO_QDBROWSER_SCRIPTS",
        ":".join((
            "/usr/bin/qdbrowser",
            "/usr/local/bin/qdbrowser",
            os.path.expanduser("~/.local/bin/qdbrowser"),
        ))).split(":") if p)


def qdbrowser_forwarder_allowed(
        pid: int,
        *,
        scripts: tuple[str, ...] | None = None,
        cmdline_reader=read_proc_cmdline,
) -> tuple[bool, str]:
    """Verify the calling pid is the first-party qdbrowser process.

    Returns ``(allowed, reason)``. Allowed only when the executed script
    (resolved exactly as in :func:`browser_bridge_allowed`) realpath-
    matches one of the allowlisted qdbrowser entry points. Fails closed on
    an empty allowlist, an unreadable cmdline, or a smuggled flag operand.
    """
    allowed_scripts = {
        os.path.realpath(p)
        for p in (scripts if scripts is not None else QDBROWSER_SCRIPTS)
    }
    if not allowed_scripts:
        return False, "qdbrowser-not-configured"
    cmdline = cmdline_reader(pid)
    executed_script = ""
    for arg in cmdline[1:]:
        if arg.startswith("-"):
            if arg in _VALUELESS_FLAGS:
                continue
            return False, "not-qdbrowser"
        executed_script = arg
        break
    if (not executed_script
            or os.path.realpath(executed_script) not in allowed_scripts):
        return False, "not-qdbrowser"
    return True, "qdbrowser"


def daemon_forward_allowed(
        pid: int,
        *,
        bridge_gate=browser_bridge_allowed,
        qdbrowser_gate=qdbrowser_forwarder_allowed,
) -> tuple[bool, str]:
    """Combined forward-parent gate used by the Phase-9e daemons.

    Accepts a forward from EITHER the native-messaging browser bridge
    (Firefox/Chrome via ``browser_bridge_allowed``) OR the first-party
    qdbrowser process (``qdbrowser_forwarder_allowed``). Both are
    kernel-attested executed-script checks; anything else fails closed
    with the bridge gate's reason (so existing audit lines are unchanged
    for the non-qdbrowser case).
    """
    ok, reason = bridge_gate(pid)
    if ok:
        return True, reason
    ok2, reason2 = qdbrowser_gate(pid)
    if ok2:
        return True, reason2
    # Surface the bridge-gate reason by default; it is the primary path.
    return False, reason


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
