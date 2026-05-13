"""qdistro-pwd polkit gate tests.

Pure unit. Mocks the dbus.SystemBus → polkitd interaction so we can
exercise both the allow/deny and no-agent paths without an actual
polkitd running.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from qdistro_pwd_polkit import (  # type: ignore[import-not-found]
    ACTION_UNLOCK, ADMIN_UID, PolkitDenied, PolkitNoAgent,
    check_unlock, is_required,
)


# -- env-var gate ------------------------------------------------------------

def test_is_required_default_on(monkeypatch):
    monkeypatch.delenv("QDISTRO_PWD_POLKIT_REQUIRED", raising=False)
    assert is_required() is True


def test_is_required_off_when_zero(monkeypatch):
    monkeypatch.setenv("QDISTRO_PWD_POLKIT_REQUIRED", "0")
    assert is_required() is False


def test_is_required_on_for_other_values(monkeypatch):
    monkeypatch.setenv("QDISTRO_PWD_POLKIT_REQUIRED", "yes")
    assert is_required() is True


# -- bypass paths ------------------------------------------------------------

def test_admin_uid_bypasses_polkit(monkeypatch):
    """uid 1000 / admin never hits the polkit interface — daemon's
    own uid is implicitly authoritative."""
    monkeypatch.setenv("QDISTRO_PWD_POLKIT_REQUIRED", "1")
    fake_bus = MagicMock()
    allowed, reason = check_unlock(ADMIN_UID, 9999, "admin-vault",
                                   bus=fake_bus)
    assert allowed is True
    assert reason == "admin-bypass"
    fake_bus.get_object.assert_not_called()


def test_disabled_gate_bypasses(monkeypatch):
    monkeypatch.setenv("QDISTRO_PWD_POLKIT_REQUIRED", "0")
    fake_bus = MagicMock()
    allowed, reason = check_unlock(1500, 9999, "vault", bus=fake_bus)
    assert allowed is True
    assert reason == "polkit-not-required"
    fake_bus.get_object.assert_not_called()


# -- polkit roundtrips -------------------------------------------------------

def _make_fake_bus(check_result):
    """Build a fake dbus.SystemBus whose CheckAuthorization returns
    `check_result` (a tuple matching polkitd's response shape)."""
    fake_ifc = MagicMock()
    fake_ifc.CheckAuthorization.return_value = check_result
    fake_proxy = MagicMock()
    fake_bus = MagicMock()
    fake_bus.get_object.return_value = fake_proxy
    return fake_bus, fake_ifc


def test_allow_returns_polkit_allow(monkeypatch):
    monkeypatch.setenv("QDISTRO_PWD_POLKIT_REQUIRED", "1")
    fake_bus, fake_ifc = _make_fake_bus((True, False, {}))
    with patch("qdistro_pwd_polkit.dbus.Interface", return_value=fake_ifc):
        allowed, reason = check_unlock(1500, 1234, "work",
                                       bus=fake_bus)
    assert allowed is True
    assert reason == "polkit-allow"
    args, _ = fake_ifc.CheckAuthorization.call_args
    # subject, action, details, flags, cancellation_id
    assert args[1] == ACTION_UNLOCK
    details = dict(args[2])
    assert details["vault"] == "work"
    assert details["caller-pid"] == "1234"
    assert details["caller-uid"] == "1500"


def test_allow_keep_returns_polkit_allow_keep(monkeypatch):
    monkeypatch.setenv("QDISTRO_PWD_POLKIT_REQUIRED", "1")
    res = (True, False, {"polkit.retains_authorization_after_challenge": "true"})
    fake_bus, fake_ifc = _make_fake_bus(res)
    with patch("qdistro_pwd_polkit.dbus.Interface", return_value=fake_ifc):
        allowed, reason = check_unlock(1500, 1234, "work", bus=fake_bus)
    assert allowed is True
    assert reason == "polkit-allow-keep"


def test_no_agent_raises(monkeypatch):
    monkeypatch.setenv("QDISTRO_PWD_POLKIT_REQUIRED", "1")
    # (authorized=False, challenge=True) is polkit's "would have prompted
    # but no agent" signal in the synchronous CheckAuthorization path.
    fake_bus, fake_ifc = _make_fake_bus((False, True, {}))
    with patch("qdistro_pwd_polkit.dbus.Interface", return_value=fake_ifc):
        with pytest.raises(PolkitNoAgent):
            check_unlock(1500, 1234, "work", bus=fake_bus)


def test_denied_raises(monkeypatch):
    monkeypatch.setenv("QDISTRO_PWD_POLKIT_REQUIRED", "1")
    fake_bus, fake_ifc = _make_fake_bus((False, False, {}))
    with patch("qdistro_pwd_polkit.dbus.Interface", return_value=fake_ifc):
        with pytest.raises(PolkitDenied):
            check_unlock(1500, 1234, "work", bus=fake_bus)


def test_caller_exe_passed_in_details(monkeypatch):
    monkeypatch.setenv("QDISTRO_PWD_POLKIT_REQUIRED", "1")
    fake_bus, fake_ifc = _make_fake_bus((True, False, {}))
    with patch("qdistro_pwd_polkit.dbus.Interface", return_value=fake_ifc):
        check_unlock(1500, 1234, "work",
                     caller_exe="/usr/bin/firefox",
                     bus=fake_bus)
    args, _ = fake_ifc.CheckAuthorization.call_args
    details = dict(args[2])
    assert details["caller-exe"] == "/usr/bin/firefox"


def test_subject_is_unix_process(monkeypatch):
    monkeypatch.setenv("QDISTRO_PWD_POLKIT_REQUIRED", "1")
    fake_bus, fake_ifc = _make_fake_bus((True, False, {}))
    with patch("qdistro_pwd_polkit.dbus.Interface", return_value=fake_ifc):
        check_unlock(1500, 4242, "v", bus=fake_bus)
    args, _ = fake_ifc.CheckAuthorization.call_args
    subject = args[0]
    assert subject[0] == "unix-process"
    subj_dict = dict(subject[1])
    assert int(subj_dict["pid"]) == 4242
