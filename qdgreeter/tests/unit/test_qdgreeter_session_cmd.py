"""Regression guard for DEFAULT_SESSION_CMD (controller.py).

greetd execs DEFAULT_SESSION_CMD as the authenticated user and treats
its lifetime AS the session lifetime. A bare
``systemctl --user start ...target`` returns 0 the instant the job is
enqueued, so greetd would see a clean session end milliseconds after
auth and recycle straight back to the greeter. The fix (see the
docstring at controller.py:42-47) is to point at the launcher wrapper
(deploy/qdwin-session-launcher.sh) which has the right ``--wait``
semantics and surfaces non-zero exits.

This one-liner pins that invariant: if someone "simplifies" the default
back to a fire-and-forget systemctl call, this fails.
"""

from __future__ import annotations

import os
import sys

import pytest

_HEADLESS = sys.platform.startswith("linux") and not os.environ.get("DISPLAY")
if _HEADLESS:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6", reason="PyQt6 not installed")

from qdgreeter.controller import DEFAULT_SESSION_CMD  # noqa: E402


@pytest.mark.cheat_aware(
    protects="the default session command targets the launcher wrapper "
    "(its lifetime IS the session), not a fire-and-forget systemctl start",
    severity="high",
    cheats=[
        "assert DEFAULT_SESSION_CMD is truthy (passes for any value)",
        "point the default at a bare `systemctl --user start ...target`",
    ],
    consequence="greetd would see the session end the instant the systemctl "
    "job is enqueued and recycle back to the greeter right after auth",
)
def test_default_session_cmd_uses_launcher_wrapper():
    joined = " ".join(DEFAULT_SESSION_CMD)
    assert "qdwin-session-launcher" in joined, (
        f"DEFAULT_SESSION_CMD must point at the launcher wrapper, got {DEFAULT_SESSION_CMD!r}"
    )
