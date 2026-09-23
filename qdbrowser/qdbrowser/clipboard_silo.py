"""qdbrowser clipboard silo shim.

The qdshell ``ClipboardGate.qml`` looks up a "source silo" from a
handle → silo map populated by ``toplevel_security_context`` events.
The compositor only emits that event for clients which bound
``wp_security_context_v1`` — i.e., processes spawned through
``qdistro-tier2-spawn``. qdbrowser launched directly from a user
silo doesn't bind that protocol today.

For the **Phase-D** wire-up we:

  1. Read ``$QDISTRO_SILO`` (the launcher-injected silo tag — same
     convention the SDK uses in :mod:`qdistro_app`).
  2. Stamp it onto every top-level window's window-title suffix
     **only when the env var is set AND the user hasn't asked us to
     hide it** (env ``QDISTRO_HIDE_SILO_BADGE=1``). The ClipboardGate
     fallback path doesn't read this — but the qdshell window-badge
     does, and it gives the user a visible reminder that the
     browser is running in a silo.
  3. Provide the silo to the App1 SDK and the autofill orchestrator
     so the compositor popup can surface it.

The "set the silo into wp_security_context_v1.instance_id" path is
a follow-up that requires either a launcher change (qdistro-tier2-spawn
already does the right thing — qdbrowser launched from there gets the
silo tag automatically) or a qdbrowser bind of the protocol. See
``plan2/research/qdbrowser-clipboard-silo-tag.md`` for the open
question.

This module is import-safe everywhere (no Qt import at module load);
the title-stamping helper takes a window-like object so unit tests
don't need a QApplication.
"""
from __future__ import annotations

import os
import re

_VALID_SILO_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,31}$")


def _sanitize_silo(s: str) -> str:
    """Return ``s`` if it matches the silo grammar, else ``""``.

    Silo names come from ``$QDISTRO_SILO`` (admin-controlled in
    production), but the title-bar badge ends up in QML
    title-formatters; an out-of-grammar silo with markup characters
    could mislead the user. L3 review — constrain to a tight
    grammar before the badge is rendered.
    """
    if not s:
        return ""
    return s if _VALID_SILO_RE.fullmatch(s) else ""


def current_silo() -> str:
    """Return the current silo tag or ``""`` if unset.

    Reads ``$QDISTRO_SILO`` first, then falls back to the unix
    username. Mirrors :func:`qdistro_app._resolve_silo`'s preference
    order so qdbrowser's silo tag agrees with the App1 receiver's.
    Sanitises against ``^[A-Za-z][A-Za-z0-9_.-]{0,31}$`` so a
    malicious env-var can't paint markup into the window title.
    """
    env = os.environ.get("QDISTRO_SILO", "").strip()
    if env:
        return _sanitize_silo(env)
    try:
        import getpass
        return _sanitize_silo(getpass.getuser())
    except Exception:  # noqa: BLE001
        return ""


def profile_silo_segment() -> str:
    """Return the sanitized ``$QDISTRO_SILO`` tag for use as a *storage*
    path segment, or ``""`` when no silo is set.

    Unlike :func:`current_silo` this does NOT fall back to the unix
    username: the persistent-profile isolation boundary is the silo tag the
    launcher injects. Silos that share ``$HOME`` share a username, so the
    username cannot distinguish them — only ``$QDISTRO_SILO`` can. Nesting
    the on-disk profile under a silo only when one is actually set lets plain
    standalone use keep the legacy flat profile path (no migration). The
    value is constrained to the silo grammar, so it is always a safe single
    path component (no ``/`` or ``..`` traversal).
    """
    return _sanitize_silo(os.environ.get("QDISTRO_SILO", "").strip())


def silo_badge_text(silo: str) -> str:
    """Build the title-bar badge for ``silo``. Empty silo → empty
    string so the caller can pass the result through ``f"{title}{badge}"``
    without a conditional."""
    if not silo:
        return ""
    return f"  —  [{silo}]"


def stamp_title(window, base_title: str, *,
                hide_badge: bool | None = None) -> str:
    """Compute the new title and apply it to ``window``.

    ``hide_badge=None`` (default) reads ``$QDISTRO_HIDE_SILO_BADGE``.
    Returns the new title string for callers that want to skip the
    setWindowTitle call (tests do this).
    """
    if hide_badge is None:
        hide_badge = (os.environ.get("QDISTRO_HIDE_SILO_BADGE", "").strip()
                      == "1")
    silo = current_silo() if not hide_badge else ""
    new_title = f"{base_title}{silo_badge_text(silo)}"
    try:
        if hasattr(window, "setWindowTitle"):
            window.setWindowTitle(new_title)
    except Exception:  # noqa: BLE001
        pass
    return new_title


def clipboard_origin_tag() -> dict:
    """Build a structured "origin" dict used by content scripts +
    the bridge_adapter to advertise where a clipboard write came from.

    Shape:

      ``{"silo": <string>, "app_id": "qdbrowser",
         "uid": <effective uid>}``

    The bridge_adapter optionally forwards this on `selection_set`
    so the compositor's `toplevel_security_context` fallback can
    correlate by-app even when the wp_security_context_v1 bind didn't
    fire. ClipboardGate.qml's allow-by-app rule reads ``app_id``.
    """
    return {
        "silo": current_silo(),
        "app_id": "qdbrowser",
        "uid": int(os.geteuid()),
    }
