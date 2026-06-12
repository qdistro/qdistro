"""Unit tests for qdlocker.app config-load hardening.

The screen locker reads /etc/qdistro/locker.conf to decide its idle
timeout, lid action, and fprintd behaviour. A config an unprivileged
user can control is a privilege/auth surface (e.g. disabling fprintd or
stretching the idle timeout). These tests exercise the three defenses:

  * _system_config_is_trusted — regular-file + root-owned + not
    group/world-writable.
  * _read_toml_no_follow      — O_NOFOLLOW so a symlink swap fails closed.
  * _validate_config          — schema/type/range filtering.

Ownership checks (st_uid) are monkeypatched on os.lstat so the suite
does not need to actually create root-owned files.
"""

from __future__ import annotations

import errno
import os
import stat
import tomllib
from types import SimpleNamespace

import pytest
from qdlocker.app import (
    _DEFAULT_CONFIG,
    _read_toml_no_follow,
    _system_config_is_trusted,
    _validate_config,
)

from qdlocker import app

# ---- _system_config_is_trusted --------------------------------------------


def _fake_stat(*, uid, mode):
    return SimpleNamespace(st_uid=uid, st_mode=mode)


def test_system_config_root_owned_is_trusted(monkeypatch, tmp_path):
    p = tmp_path / "locker.conf"
    p.write_text("idle_timeout_s = 60\n")
    monkeypatch.setattr(
        os, "lstat",
        lambda path: _fake_stat(uid=0, mode=stat.S_IFREG | 0o644),
    )
    assert _system_config_is_trusted(str(p)) is True


@pytest.mark.cheat_aware(
    protects="a system config NOT owned by root is rejected — an "
    "unprivileged user cannot plant /etc/qdistro/locker.conf to weaken "
    "the locker (disable fprintd, stretch the idle timeout)",
    severity="high",
    cheats=[
        "drop the st_uid==0 check",
        "accept any uid that can read the file",
        "stat the realpath after following a symlink to a root-owned file",
    ],
    consequence="a non-root user controls the locker's security policy via "
    "a config file they own — an auth/privilege bypass surface",
)
def test_system_config_user_owned_not_trusted(monkeypatch, tmp_path):
    p = tmp_path / "locker.conf"
    p.write_text("idle_timeout_s = 60\n")
    monkeypatch.setattr(
        os, "lstat",
        lambda path: _fake_stat(uid=1000, mode=stat.S_IFREG | 0o644),
    )
    assert _system_config_is_trusted(str(p)) is False


@pytest.mark.cheat_aware(
    protects="a root-owned but group/world-writable system config is "
    "rejected — a non-root user in the right group cannot weaken the locker "
    "by writing to it",
    severity="high",
    cheats=[
        "check only st_uid==0 and ignore the writable mode bits",
    ],
    consequence="any user who can write the file controls locker policy "
    "despite root ownership",
)
def test_system_config_group_world_writable_not_trusted(monkeypatch, tmp_path):
    p = tmp_path / "locker.conf"
    p.write_text("idle_timeout_s = 60\n")
    monkeypatch.setattr(
        os, "lstat",
        lambda path: _fake_stat(uid=0, mode=stat.S_IFREG | 0o666),
    )
    assert _system_config_is_trusted(str(p)) is False


@pytest.mark.cheat_aware(
    protects="a non-regular file (symlink/fifo) at the config path is "
    "rejected before its contents are trusted",
    severity="high",
    cheats=[
        "trust any path that stats with uid==0, including a symlink",
        "follow the symlink and stat the (root-owned) target",
    ],
    consequence="a symlink swap redirects the trusted-config read to "
    "attacker-controlled content",
)
def test_system_config_non_regular_file_not_trusted(monkeypatch):
    # e.g. a symlink (S_IFLNK) or fifo at the config path.
    monkeypatch.setattr(
        os, "lstat",
        lambda path: _fake_stat(uid=0, mode=stat.S_IFLNK | 0o777),
    )
    assert _system_config_is_trusted("/etc/qdistro/locker.conf") is False


def test_system_config_missing_not_trusted(monkeypatch):
    def raise_oserror(path):
        raise FileNotFoundError(path)

    monkeypatch.setattr(os, "lstat", raise_oserror)
    assert _system_config_is_trusted("/nonexistent/locker.conf") is False


# ---- _read_toml_no_follow (O_NOFOLLOW) ------------------------------------


def test_read_toml_no_follow_reads_regular_file(tmp_path):
    p = tmp_path / "locker.conf"
    p.write_text('lid_action = "ignore"\nidle_timeout_s = 120\n')
    data = _read_toml_no_follow(str(p))
    assert data == {"lid_action": "ignore", "idle_timeout_s": 120}


@pytest.mark.cheat_aware(
    protects="opening the config with O_NOFOLLOW rejects a symlink at the "
    "config path — a symlink-swap (point locker.conf at an attacker file) "
    "fails closed instead of being read",
    severity="high",
    cheats=[
        "open the path without O_NOFOLLOW",
        "resolve/realpath the symlink and read the target anyway",
        "catch the resulting OSError and silently fall through to a follow",
    ],
    consequence="an attacker who can create a symlink at the config path "
    "redirects the locker to read arbitrary content, controlling security "
    "policy — a symlink-swap config-injection bypass",
)
def test_read_toml_no_follow_rejects_symlink(tmp_path):
    target = tmp_path / "real.conf"
    target.write_text('lid_action = "ignore"\n')
    link = tmp_path / "locker.conf"
    link.symlink_to(target)

    with pytest.raises(OSError) as exc:
        _read_toml_no_follow(str(link))
    # O_NOFOLLOW on a symlink raises ELOOP on Linux (errno 40). The key
    # invariant is that the open fails closed rather than reading the
    # target — never that tomllib returned the target's contents.
    assert exc.value.errno == errno.ELOOP


def test_read_toml_no_follow_propagates_decode_error(tmp_path):
    p = tmp_path / "locker.conf"
    p.write_text("this is = = not valid toml ===\n")
    with pytest.raises(tomllib.TOMLDecodeError):
        _read_toml_no_follow(str(p))


# ---- _validate_config ------------------------------------------------------


def test_validate_config_accepts_valid_values():
    src = {
        "idle_timeout_s": 600,
        "lid_action": "ignore",
        "fprintd_enabled": False,
        "fprintd_max_failures": 5,
        "fprintd_timeout_s": 30,
    }
    cleaned = _validate_config(src, "test")
    assert cleaned == src


def test_validate_config_drops_unknown_keys():
    cleaned = _validate_config({"bogus_key": 1, "idle_timeout_s": 60}, "test")
    assert cleaned == {"idle_timeout_s": 60}
    assert "bogus_key" not in cleaned


def test_validate_config_drops_wrong_type():
    # idle_timeout_s must be int, not str.
    cleaned = _validate_config({"idle_timeout_s": "300"}, "test")
    assert cleaned == {}


def test_validate_config_rejects_bool_for_int_key():
    # bool is a subtype of int in Python; the schema explicitly rejects it
    # for an int key so True doesn't sneak in as idle_timeout_s == 1.
    cleaned = _validate_config({"idle_timeout_s": True}, "test")
    assert cleaned == {}


def test_validate_config_drops_out_of_range():
    # idle_timeout_s range is 0 < v <= 86400.
    assert _validate_config({"idle_timeout_s": 0}, "test") == {}
    assert _validate_config({"idle_timeout_s": 999999}, "test") == {}
    assert _validate_config({"idle_timeout_s": 300}, "test") == {"idle_timeout_s": 300}


def test_validate_config_rejects_bad_lid_action():
    assert _validate_config({"lid_action": "explode"}, "test") == {}
    assert _validate_config({"lid_action": "lock"}, "test") == {"lid_action": "lock"}


def test_validate_config_fprintd_failures_range():
    assert _validate_config({"fprintd_max_failures": 0}, "test") == {}
    assert _validate_config({"fprintd_max_failures": 101}, "test") == {}
    assert _validate_config({"fprintd_max_failures": 3}, "test") == {
        "fprintd_max_failures": 3
    }


# ---- load_config integration (end-to-end of the three defenses) -----------


def test_load_config_defaults_when_no_files(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda path: False)
    cfg = app.load_config()
    assert cfg == _DEFAULT_CONFIG


def test_load_config_uses_trusted_system_config(monkeypatch, tmp_path):
    real = tmp_path / "system.conf"
    real.write_text("idle_timeout_s = 42\n")

    monkeypatch.setattr(
        os.path, "exists",
        lambda path: path == "/etc/qdistro/locker.conf",
    )
    monkeypatch.setattr(app, "_system_config_is_trusted", lambda path: True)
    monkeypatch.setattr(app, "_read_toml_no_follow", lambda path: {"idle_timeout_s": 42})

    cfg = app.load_config()
    assert cfg["idle_timeout_s"] == 42


@pytest.mark.cheat_aware(
    protects="an untrusted system config is NOT loaded and the locker does "
    "not fall back to a user config — it uses built-in defaults instead",
    severity="high",
    cheats=[
        "fall back to the user config when the system config is untrusted",
        "load the untrusted system config anyway",
    ],
    consequence="an attacker-controlled config silently replaces the "
    "locker's security policy",
)
def test_load_config_refuses_untrusted_system_config(monkeypatch):
    # System config exists but is untrusted -> defaults, NO user fallback.
    monkeypatch.setattr(
        os.path, "exists",
        lambda path: path in (
            "/etc/qdistro/locker.conf",
            os.path.expanduser("~/.config/qdistro/locker.conf"),
        ),
    )
    monkeypatch.setattr(app, "_system_config_is_trusted", lambda path: False)

    def must_not_read(path):
        raise AssertionError(f"read attempted on untrusted/fallback path: {path}")

    monkeypatch.setattr(app, "_read_toml_no_follow", must_not_read)

    cfg = app.load_config()
    assert cfg == _DEFAULT_CONFIG


def test_load_config_invalid_toml_uses_defaults(monkeypatch):
    monkeypatch.setattr(
        os.path, "exists",
        lambda path: path == "/etc/qdistro/locker.conf",
    )
    monkeypatch.setattr(app, "_system_config_is_trusted", lambda path: True)

    def raise_decode(path):
        raise tomllib.TOMLDecodeError("bad", "doc", 0)

    monkeypatch.setattr(app, "_read_toml_no_follow", raise_decode)

    cfg = app.load_config()
    assert cfg == _DEFAULT_CONFIG
