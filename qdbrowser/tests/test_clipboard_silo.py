"""Tests for the clipboard_silo helper (Phase-D shim).

No QApplication required — the helper takes a duck-typed window with
just ``setWindowTitle``.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from qdbrowser import clipboard_silo as cs


class TestCurrentSilo:
    def test_env_var(self, monkeypatch):
        monkeypatch.setenv("QDISTRO_SILO", "work")
        assert cs.current_silo() == "work"

    def test_trailing_newline_rejected(self, monkeypatch):
        # `re.match` + `$` used to accept "work\n" as a distinct silo
        # (iso2 `13` E5); fullmatch rejects it.
        monkeypatch.setenv("QDISTRO_SILO", "work\n")
        assert cs.current_silo() != "work\n"
        assert cs._sanitize_silo("work\n") == ""
        assert cs._sanitize_silo("work") == "work"

    def test_no_env_falls_back_to_user(self, monkeypatch):
        monkeypatch.delenv("QDISTRO_SILO", raising=False)
        s = cs.current_silo()
        # Falls back to the unix username — non-empty in any test env.
        assert isinstance(s, str)
        assert len(s) > 0


class TestBadge:
    def test_empty_silo_no_badge(self):
        assert cs.silo_badge_text("") == ""

    def test_silo_wraps_in_brackets(self):
        assert cs.silo_badge_text("work").endswith("[work]")

    def test_stamp_title_adds_badge(self, monkeypatch):
        monkeypatch.setenv("QDISTRO_SILO", "work")
        win = MagicMock()
        out = cs.stamp_title(win, "example.com — qdbrowser")
        assert out.endswith("[work]")
        win.setWindowTitle.assert_called_once_with(out)

    def test_stamp_title_hide_via_env(self, monkeypatch):
        monkeypatch.setenv("QDISTRO_SILO", "work")
        monkeypatch.setenv("QDISTRO_HIDE_SILO_BADGE", "1")
        win = MagicMock()
        out = cs.stamp_title(win, "example.com — qdbrowser")
        assert "[work]" not in out
        assert out == "example.com — qdbrowser"

    def test_stamp_title_no_setwindowtitle_does_not_raise(self):
        # If the window doesn't have setWindowTitle we still return
        # the computed title (caller can use it for logging).
        out = cs.stamp_title(object(), "x")
        assert out.startswith("x")


class TestOriginTag:
    def test_origin_tag_shape(self, monkeypatch):
        monkeypatch.setenv("QDISTRO_SILO", "personal")
        tag = cs.clipboard_origin_tag()
        assert tag["silo"] == "personal"
        assert tag["app_id"] == "qdbrowser"
        assert isinstance(tag["uid"], int)
