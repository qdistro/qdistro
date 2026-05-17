"""qdistro-polkit-agent — method dispatch + helper tests.

Covers:
- ``select_method``: env override > config glob > default.
- ``load_method_config``: file format + sanitisation.
- ``_pam_authenticate``: success / failure / missing python-pam.
- ``_prompt_password``: noninteractive shortcuts (allow / deny /
  password=).
- ``_sanitize_polkit_details``: ANSI/control-char strip + length cap.

These are pure / mock-only; no actual polkit / dbus / PAM is touched.
The dbus + GLib import is expected — qdistro_polkit_agent.py is the
agent module proper.
"""
from __future__ import annotations

import os
import sys
from unittest import mock

import pytest

# Stub out python-pam so the agent imports cleanly even when
# python-pam isn't installed on the host runner.
sys.modules.setdefault("pam", mock.MagicMock())  # noqa: F401

from qdistro_polkit_agent import (  # noqa: E402
    DEFAULT_METHOD, _pam_authenticate, _prompt_password,
    _sanitize_polkit_details, _scrub_value,
    load_method_config, select_method,
)


# -- select_method -----------------------------------------------------

class TestSelectMethod:
    def test_default_is_broker(self):
        assert select_method("com.example.foo", [], env={}) == "broker"
        assert DEFAULT_METHOD == "broker"

    def test_env_override_wins(self):
        cfg = [("com.example.*", "broker")]
        assert select_method("com.example.foo", cfg,
                             env={"QDISTRO_POLKIT_METHOD": "pam"}) == "pam"
        assert select_method("com.example.foo", cfg,
                             env={"QDISTRO_POLKIT_METHOD": "fprint"}) == "fprint"

    def test_env_override_invalid_falls_through(self):
        cfg = [("com.example.*", "pam")]
        # Garbage env value → ignored, config glob applies.
        assert select_method("com.example.foo", cfg,
                             env={"QDISTRO_POLKIT_METHOD": "garbage"}) == "pam"

    def test_first_glob_wins(self):
        cfg = [
            ("org.qdistro.pwd.*", "pam"),
            ("org.qdistro.*",     "broker"),
        ]
        assert select_method("org.qdistro.pwd.unlock", cfg, env={}) == "pam"
        assert select_method("org.qdistro.print.add",  cfg, env={}) == "broker"

    def test_no_match_falls_to_default(self):
        cfg = [("org.qdistro.pwd.*", "pam")]
        assert select_method("org.freedesktop.NetworkManager.settings.modify",
                             cfg, env={}) == "broker"

    def test_glob_matches_dot_pattern(self):
        cfg = [("*pwd*", "fprint")]
        assert select_method("org.qdistro.pwd.unlock", cfg, env={}) == "fprint"


# -- load_method_config ------------------------------------------------

class TestLoadMethodConfig:
    def test_missing_file_returns_empty(self, tmp_path):
        out = load_method_config(str(tmp_path / "absent.conf"))
        assert out == []

    def test_parses_basic_lines(self, tmp_path):
        p = tmp_path / "cfg"
        p.write_text(
            "# comment\n"
            "org.qdistro.pwd.* = pam\n"
            "org.qdistro.print.* = broker\n"
            "\n"
            "* = fprint\n"
        )
        out = load_method_config(str(p))
        assert out == [
            ("org.qdistro.pwd.*",   "pam"),
            ("org.qdistro.print.*", "broker"),
            ("*",                   "fprint"),
        ]

    def test_invalid_method_skipped(self, tmp_path):
        p = tmp_path / "cfg"
        p.write_text(
            "good.glob = pam\n"
            "bad.glob = garbage\n"   # unknown method → skipped
            "missing.equals\n"        # no = → skipped
        )
        out = load_method_config(str(p))
        assert out == [("good.glob", "pam")]

    def test_method_lowercased(self, tmp_path):
        p = tmp_path / "cfg"
        p.write_text("foo = PAM\nbar = Broker\n")
        out = load_method_config(str(p))
        assert out == [("foo", "pam"), ("bar", "broker")]


# -- _pam_authenticate -------------------------------------------------

class TestPamAuthenticate:
    def test_success(self):
        m = mock.MagicMock()
        m.pam.return_value.authenticate.return_value = True
        with mock.patch.dict(sys.modules, {"pam": m}):
            ok, reason = _pam_authenticate("testuser", "testpw")
        assert ok is True
        assert reason == "pam-ok"

    def test_failure(self):
        m = mock.MagicMock()
        m.pam.return_value.authenticate.return_value = False
        m.pam.return_value.reason = "Authentication failure"
        with mock.patch.dict(sys.modules, {"pam": m}):
            ok, reason = _pam_authenticate("admin", "wrong")
        assert ok is False
        assert reason == "Authentication failure"

    def test_missing_python_pam(self):
        # ImportError path: drop pam from sys.modules + re-raise on
        # import. mock.patch.dict on builtins.__import__ is the stable
        # way to do this in pytest.
        with mock.patch.dict(sys.modules, {"pam": None}):
            ok, reason = _pam_authenticate("admin", "anything")
        assert ok is False
        assert "not installed" in reason

    def test_pam_crash_failed_closed(self):
        m = mock.MagicMock()
        m.pam.return_value.authenticate.side_effect = RuntimeError("boom")
        with mock.patch.dict(sys.modules, {"pam": m}):
            ok, reason = _pam_authenticate("admin", "x")
        assert ok is False
        assert "pam-error" in reason


# -- _prompt_password (noninteractive shortcuts) -----------------------

class TestPromptNonInteractive:
    def test_deny(self):
        out = _prompt_password("act", "msg",
                               env={"QDISTRO_POLKIT_NONINTERACTIVE": "deny"})
        assert out is None

    def test_allow(self):
        out = _prompt_password("act", "msg",
                               env={"QDISTRO_POLKIT_NONINTERACTIVE": "allow"})
        assert out == ""

    def test_password_passthrough(self):
        out = _prompt_password("act", "msg",
                               env={"QDISTRO_POLKIT_NONINTERACTIVE": "password=hunter2"})
        assert out == "hunter2"

    def test_no_prompt_bin_no_override_fails_closed(self, tmp_path):
        absent = str(tmp_path / "no-such-prompt")
        out = _prompt_password("act", "msg",
                               prompt_bin=absent, env={})
        assert out is None


# -- _sanitize_polkit_details ------------------------------------------

class TestSanitiseDetails:
    def test_strips_control_chars(self):
        raw = {"k\x01ey": "val\x07ue"}
        out = _sanitize_polkit_details(raw)
        assert out == {"key": "value"}

    def test_caps_value_length(self):
        raw = {"k": "x" * 5000}
        out = _sanitize_polkit_details(raw)
        assert len(out["k"]) == 512

    def test_caps_key_count(self):
        raw = {f"k{i}": "v" for i in range(50)}
        out = _sanitize_polkit_details(raw)
        assert len(out) == 16

    def test_ansi_escapes_dropped(self):
        # \x1b is non-printable → stripped.
        out = _scrub_value("hello\x1b[31mred\x1b[0m")
        assert out == "hello[31mred[0m"  # only the ESC byte goes


# -- end ---------------------------------------------------------------
