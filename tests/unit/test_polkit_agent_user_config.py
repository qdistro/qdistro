"""Layered (user + system) polkit agent config — task(108).

Adds a per-user override at ``~/.config/qdistro/polkit-agent.conf`` that
the admin app's Polkit tab writes. Pure unit; no D-Bus.
"""
from __future__ import annotations

import os
import pytest

from qdistro_polkit_agent import (  # type: ignore[import-not-found]
    load_method_config, load_method_config_layered,
    render_user_config, save_user_config, select_method,
)


# -- Layered loader -------------------------------------------------------

class TestLayered:
    def test_user_only(self, tmp_path):
        u = tmp_path / "user.conf"
        u.write_text("foo.* = pam\n")
        # Pass a non-existent system path so layered returns just user.
        out = load_method_config_layered(
            user_path=str(u), system_path=str(tmp_path / "absent.conf"))
        assert out == [("foo.*", "pam")]

    def test_system_only(self, tmp_path):
        s = tmp_path / "system.conf"
        s.write_text("bar.* = broker\n")
        out = load_method_config_layered(
            user_path=str(tmp_path / "absent.conf"), system_path=str(s))
        assert out == [("bar.*", "broker")]

    def test_user_first_then_system(self, tmp_path):
        u = tmp_path / "user.conf"
        u.write_text("foo.* = fprint\n")
        s = tmp_path / "system.conf"
        s.write_text("foo.* = pam\nbaz.* = broker\n")
        out = load_method_config_layered(
            user_path=str(u), system_path=str(s))
        # User entry comes first, so first-match-wins picks user's
        # `fprint` for foo.* but still falls through to system's
        # `broker` for baz.*.
        assert out == [("foo.*", "fprint"),
                       ("foo.*", "pam"),
                       ("baz.*", "broker")]

    def test_user_override_wins_in_select(self, tmp_path):
        u = tmp_path / "user.conf"
        u.write_text("org.qdistro.pwd.* = fprint\n")
        s = tmp_path / "system.conf"
        s.write_text("org.qdistro.pwd.* = pam\n")
        out = load_method_config_layered(
            user_path=str(u), system_path=str(s))
        assert select_method("org.qdistro.pwd.unlock", out) == "fprint"

    def test_default_user_path_expands_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        # No file present → empty result, no exception.
        out = load_method_config_layered(
            user_path="~/.config/qdistro/polkit-agent.conf",
            system_path=str(tmp_path / "absent.conf"))
        assert out == []


# -- Renderer -------------------------------------------------------------

class TestRender:
    def test_basic_lines(self):
        body = render_user_config([("foo.*", "pam"), ("bar.*", "broker")])
        assert "foo.* = pam" in body
        assert "bar.* = broker" in body
        # Header always present.
        assert body.startswith("#")

    def test_drops_invalid_method(self):
        body = render_user_config([("foo.*", "pam"), ("bad", "wrong")])
        assert "foo.* = pam" in body
        assert "wrong" not in body

    def test_drops_empty_glob(self):
        body = render_user_config([("", "pam"), ("foo.*", "pam")])
        # Only the valid entry survives.
        lines = [l for l in body.splitlines()
                 if l and not l.startswith("#")]
        assert lines == ["foo.* = pam"]

    def test_lowercase_method(self):
        body = render_user_config([("foo.*", "PAM")])
        assert "foo.* = pam" in body
        assert "PAM" not in body

    def test_empty_entries_yields_header_only(self):
        body = render_user_config([])
        # No body lines.
        body_lines = [l for l in body.splitlines() if l and not l.startswith("#")]
        assert body_lines == []

    def test_round_trip_through_loader(self, tmp_path):
        body = render_user_config([("a.*", "pam"), ("b.*", "fprint")])
        p = tmp_path / "out.conf"
        p.write_text(body)
        assert load_method_config(str(p)) == [
            ("a.*", "pam"), ("b.*", "fprint")]


# -- Atomic save ----------------------------------------------------------

class TestSave:
    def test_writes_and_creates_parent(self, tmp_path):
        path = tmp_path / "subdir" / "polkit-agent.conf"
        save_user_config([("foo.*", "pam")], path=str(path))
        assert path.exists()
        assert "foo.* = pam" in path.read_text()
        # Parent dir was created.
        assert path.parent.is_dir()

    def test_overwrites_existing(self, tmp_path):
        path = tmp_path / "polkit-agent.conf"
        save_user_config([("foo.*", "pam")], path=str(path))
        save_user_config([("bar.*", "broker")], path=str(path))
        body = path.read_text()
        assert "bar.* = broker" in body
        assert "foo.* = pam" not in body

    def test_atomic_temp_cleaned(self, tmp_path):
        path = tmp_path / "polkit-agent.conf"
        save_user_config([("foo.*", "pam")], path=str(path))
        siblings = list(path.parent.iterdir())
        # Only the final file should remain (no .tmp leftover).
        assert [s.name for s in siblings] == ["polkit-agent.conf"]

    def test_returns_resolved_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        out = save_user_config(
            [("a.*", "pam")],
            path="~/.config/qdistro/polkit-agent.conf")
        assert os.path.expanduser("~/.config/qdistro/polkit-agent.conf") == out
        assert os.path.exists(out)
