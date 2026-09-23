"""Unit tests for the config auth-key trust split (finding 06).

Auth-affecting knobs (fprintd_enabled, fprintd_max_failures, fprintd_timeout_s)
must be honored ONLY from the trusted, root-owned system config — never from a
user-writable ~/.config/qdistro/locker.conf. The ergonomic knobs (idle_timeout_s,
lid_action) remain user-overridable. This holds structurally in code, even if
the root-owned /etc/qdistro/locker.conf is ever missing.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from qdlocker.app import (
    _SYSTEM_ONLY_KEYS,
    _introspection_authorized,
    _validate_config,
)


@pytest.mark.cheat_aware(
    protects="ctrl-socket introspection (the password-length side channel) is "
    "authorized ONLY by a root-owned marker, never a user-controlled env var, "
    "so a compromised same-uid process cannot re-enable it",
    severity="low-medium",
    cheats=[
        "honor a QDLOCKER_CTRL_INTROSPECTION env var",
        "accept a user-owned marker file",
        "default introspection on when the marker is absent",
    ],
    consequence="a same-uid process re-enables status/prompt-text and reads "
    "the live password length on a production locker",
)
def test_introspection_off_without_root_marker(monkeypatch):
    # Production default: the marker does not exist -> introspection refused.
    import qdlocker.app as app_mod
    monkeypatch.setattr(app_mod, "_INTROSPECTION_MARKER",
                        "/nonexistent/qdistro/locker-ctrl-introspection")
    assert _introspection_authorized() is False


def test_introspection_rejects_self_owned_marker(monkeypatch):
    # A marker owned by the service's own uid must be refused even under SAFE
    # (root-owned) parent dirs — proving the self-owned-file check itself, not
    # just a writable parent. This is the same-uid forgery case.
    import stat
    from types import SimpleNamespace

    import qdlocker.app as app_mod
    marker = "/etc/qdistro/locker-ctrl-introspection"
    monkeypatch.setattr(app_mod, "_INTROSPECTION_MARKER", marker)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(os, "getegid", lambda: 1000)
    monkeypatch.setattr(os, "getgroups", lambda: [1000])

    def fake_lstat(path):
        if str(path) == marker:
            # owned by us (the same-uid attacker)
            return SimpleNamespace(st_uid=1000, st_gid=0,
                                   st_mode=stat.S_IFREG | 0o644)
        return SimpleNamespace(st_uid=0, st_gid=0, st_mode=stat.S_IFDIR | 0o755)

    monkeypatch.setattr(os, "lstat", fake_lstat)
    assert _introspection_authorized() is False


def test_introspection_trusts_overflow_uid_marker_under_userns(monkeypatch):
    # PrivateNetwork regression: under the service userns a root-installed marker
    # reads as the overflow uid 65534 (!= our uid). It must enable introspection;
    # before the fix the st_uid==0 check refused it and the GUI harness broke.
    import stat
    from types import SimpleNamespace

    import qdlocker.app as app_mod
    marker = "/etc/qdistro/locker-ctrl-introspection"
    monkeypatch.setattr(app_mod, "_INTROSPECTION_MARKER", marker)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(os, "getegid", lambda: 1000)
    monkeypatch.setattr(os, "getgroups", lambda: [1000])

    def fake_lstat(path):
        if str(path) == marker:
            return SimpleNamespace(st_uid=65534, st_gid=0,
                                   st_mode=stat.S_IFREG | 0o644)
        return SimpleNamespace(st_uid=0, st_gid=0, st_mode=stat.S_IFDIR | 0o755)

    monkeypatch.setattr(os, "lstat", fake_lstat)
    assert _introspection_authorized() is True


@pytest.mark.cheat_aware(
    protects="auth-affecting fprintd_* knobs are dropped when they come from "
    "the untrusted user config path; only ergonomic knobs survive",
    severity="low",
    cheats=[
        "call _validate_config with allow_auth_keys=True for the user path",
        "move fprintd_* out of _SYSTEM_ONLY_KEYS",
        "honor the user file's fprintd_enabled=false",
    ],
    consequence="a user-writable ~/.config file disables fingerprint auth or "
    "widens the strike threshold on a hardened system",
)
def test_user_config_cannot_set_auth_keys():
    user_cfg = {
        "idle_timeout_s": 120,
        "lid_action": "ignore",
        "fprintd_enabled": False,
        "fprintd_max_failures": 99,
        "fprintd_timeout_s": 600,
    }
    cleaned = _validate_config(user_cfg, "~/.config/qdistro/locker.conf",
                               allow_auth_keys=False)
    # Ergonomic keys survive.
    assert cleaned["idle_timeout_s"] == 120
    assert cleaned["lid_action"] == "ignore"
    # Auth keys are dropped entirely.
    for key in _SYSTEM_ONLY_KEYS:
        assert key not in cleaned, f"{key} must not be honored from user config"


def test_system_config_honors_auth_keys():
    sys_cfg = {
        "fprintd_enabled": False,
        "fprintd_max_failures": 5,
        "fprintd_timeout_s": 30,
        "idle_timeout_s": 300,
    }
    cleaned = _validate_config(sys_cfg, "/etc/qdistro/locker.conf",
                               allow_auth_keys=True)
    assert cleaned["fprintd_enabled"] is False
    assert cleaned["fprintd_max_failures"] == 5
    assert cleaned["fprintd_timeout_s"] == 30
    assert cleaned["idle_timeout_s"] == 300


def test_fprintd_keys_are_the_system_only_set():
    # Guard against someone narrowing the protected set.
    assert _SYSTEM_ONLY_KEYS == frozenset({
        "fprintd_enabled", "fprintd_max_failures", "fprintd_timeout_s",
    })
