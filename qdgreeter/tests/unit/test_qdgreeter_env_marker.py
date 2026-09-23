"""Env-override marker gate tests (finding 05).

QDGREETER_USER / QDGREETER_SESSION_CMD decide WHO authenticates and WHAT runs
as that user after a correct password (the latter is handed to greetd
start_session). In production greetd supplies a clean root-owned environment and
does not set them, so the hardcoded defaults apply. These env overrides are
honored ONLY when a ROOT-OWNED marker authorizes them, so a polluted /
attacker-influenced environment cannot redirect the authenticated identity or
the post-auth command. A same-uid/unprivileged process cannot forge the marker.
"""

from __future__ import annotations

import os
import sys

import pytest

_HEADLESS = sys.platform.startswith("linux") and not os.environ.get("DISPLAY")
if _HEADLESS:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6", reason="PyQt6 not installed")
from qdgreeter import controller as ctl_mod  # noqa: E402
from qdgreeter.controller import (  # noqa: E402
    _env_overrides_authorized,
    _resolve_session_cmd,
    _resolve_user,
)


@pytest.mark.cheat_aware(
    protects="env overrides for the authenticated user / post-auth session "
    "command are ignored unless a ROOT-OWNED marker authorizes them; production "
    "(no marker) always uses the safe defaults",
    severity="low",
    cheats=[
        "read QDGREETER_USER/SESSION_CMD from env unconditionally",
        "accept a user-owned or world-writable marker",
        "default to honoring the env when the marker is absent",
    ],
    consequence="a polluted environment redirects WHO logs in or WHAT runs as "
    "the authenticated user after a correct password",
)
def test_env_ignored_without_marker(monkeypatch):
    monkeypatch.setattr(ctl_mod, "_ENV_OVERRIDE_MARKER", "/nonexistent/qdistro/marker")
    monkeypatch.setenv("QDGREETER_USER", "attacker")
    monkeypatch.setenv("QDGREETER_SESSION_CMD", "/bin/sh -c evil")
    assert _env_overrides_authorized() is False
    assert _resolve_user() == "admin"
    assert _resolve_session_cmd() == ["/usr/local/bin/qdwin-session-launcher"]


def test_user_owned_marker_rejected(tmp_path, monkeypatch):
    # A marker the user could create (owned by the test uid, not root) must be
    # refused — the trust rule requires uid 0.
    marker = tmp_path / "greeter-env-override"
    marker.write_text("")
    monkeypatch.setattr(ctl_mod, "_ENV_OVERRIDE_MARKER", str(marker))
    monkeypatch.setenv("QDGREETER_USER", "attacker")
    assert _env_overrides_authorized() is False
    assert _resolve_user() == "admin"


def test_symlink_marker_rejected(tmp_path, monkeypatch):
    target = tmp_path / "real"
    target.write_text("")
    link = tmp_path / "greeter-env-override"
    link.symlink_to(target)
    monkeypatch.setattr(ctl_mod, "_ENV_OVERRIDE_MARKER", str(link))
    # lstat on a symlink -> not a regular file -> refused (no symlink follow).
    assert _env_overrides_authorized() is False


def test_group_or_world_writable_marker_rejected(tmp_path, monkeypatch):
    marker = tmp_path / "greeter-env-override"
    marker.write_text("")
    marker.chmod(0o666)
    monkeypatch.setattr(ctl_mod, "_ENV_OVERRIDE_MARKER", str(marker))
    assert _env_overrides_authorized() is False


def test_session_cmd_uses_shlex_when_authorized(tmp_path, monkeypatch):
    # When the marker IS trusted, the session command is shlex-split (quoted
    # args survive). We can't make a root-owned file in a unit test, so stub the
    # authorization to exercise the resolution logic.
    monkeypatch.setattr(ctl_mod, "_env_overrides_authorized", lambda: True)
    monkeypatch.setenv("QDGREETER_SESSION_CMD", '/usr/bin/launch --flag "a b"')
    assert _resolve_session_cmd() == ["/usr/bin/launch", "--flag", "a b"]
    monkeypatch.setenv("QDGREETER_USER", "kiosk")
    assert _resolve_user() == "kiosk"


def test_malformed_authorized_session_cmd_falls_back(monkeypatch):
    # An authorized but malformed (unbalanced quote) command must not raise at
    # import / exit the only interactive login — fall back to the launcher.
    monkeypatch.setattr(ctl_mod, "_env_overrides_authorized", lambda: True)
    monkeypatch.setenv("QDGREETER_SESSION_CMD", 'foo "unterminated')
    assert _resolve_session_cmd() == ["/usr/local/bin/qdwin-session-launcher"]


def test_whitespace_only_authorized_overrides_fall_back(monkeypatch):
    monkeypatch.setattr(ctl_mod, "_env_overrides_authorized", lambda: True)
    monkeypatch.setenv("QDGREETER_SESSION_CMD", "   ")
    monkeypatch.setenv("QDGREETER_USER", "   ")
    assert _resolve_session_cmd() == ["/usr/local/bin/qdwin-session-launcher"]
    assert _resolve_user() == "admin"


def test_defaults_are_admin_and_launcher():
    # The shipped defaults (used in production where no marker exists).
    assert ctl_mod._FALLBACK_USER == "admin"
    assert ctl_mod._FALLBACK_SESSION_CMD == ["/usr/local/bin/qdwin-session-launcher"]
