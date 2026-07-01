"""Lifecycle relock coverage for qdistro-pwd."""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "pwd"))

import qdistro_pwd_daemon as d
from qdistro_pwd_audit import PwdAuditLog  # type: ignore


def _daemon(tmp_path):
    daemon = d.PwdDaemon.__new__(d.PwdDaemon)
    daemon._audit = PwdAuditLog(str(tmp_path / "audit.sqlite"))
    daemon._fill_tokens = {}
    daemon._fill_token_ttl = 120
    return daemon


def test_lock_all_wipes_all_keys_emits_signals_and_clears_fill_tokens(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "BROWSER_PWD_VAULT", "passwords")
    daemon = _daemon(tmp_path)
    work_key = bytearray(b"a" * 32)
    browser_key = bytearray(b"b" * 32)
    daemon._unlocked = {
        "work": {"key": work_key, "unlocked_at": 0, "last_use": 0},
        "passwords": {"key": browser_key, "unlocked_at": 0, "last_use": 0},
    }
    daemon._fill_tokens = {
        "tok:alice": {
            "origin": "https://example.com",
            "username": "alice",
            "pid": 123,
            "expires": 9999999999,
        }
    }
    emitted = []
    monkeypatch.setattr(daemon, "VaultLocked",
                        lambda name, reason: emitted.append((name, reason)))

    locked = daemon._lock_all_unlocked("screen-lock:manual")

    assert locked == ["work", "passwords"]
    assert daemon._unlocked == {}
    assert set(work_key) == {0}
    assert set(browser_key) == {0}
    assert daemon._fill_tokens == {}
    assert emitted == [
        ("work", "screen-lock:manual"),
        ("passwords", "screen-lock:manual"),
    ]


def test_lifecycle_lock_clears_orphaned_fill_tokens(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "BROWSER_PWD_VAULT", "passwords")
    daemon = _daemon(tmp_path)
    daemon._unlocked = {}
    daemon._fill_tokens = {
        "tok:alice": {
            "origin": "https://example.com",
            "username": "alice",
            "pid": 123,
            "expires": 9999999999,
        }
    }
    monkeypatch.setattr(daemon, "VaultLocked", lambda _name, _reason: None)

    assert daemon._lock_all_unlocked("screen-lock") == []
    assert daemon._fill_tokens == {}


def test_lock_all_vaults_method_requires_admin_or_root(tmp_path, monkeypatch):
    daemon = _daemon(tmp_path)
    daemon._unlocked = {}
    monkeypatch.setattr(daemon, "_peer_info", lambda _sender: (1500, 99))

    with pytest.raises(d.PwdPolicyError):
        daemon.LockAllVaults("screen-lock", sender=":1.9")


def test_lock_all_vaults_method_rejects_unknown_reason(tmp_path, monkeypatch):
    daemon = _daemon(tmp_path)
    daemon._unlocked = {}
    monkeypatch.setattr(daemon, "_peer_info", lambda _sender: (d.ADMIN_UID, 99))

    with pytest.raises(d.PwdPolicyError):
        daemon.LockAllVaults("surprise", sender=":1.9")


def test_logind_prepare_for_sleep_start_locks_all(tmp_path, monkeypatch):
    daemon = _daemon(tmp_path)
    daemon._unlocked = {"work": {"key": bytearray(b"x" * 32), "last_use": 0}}
    called = []
    monkeypatch.setattr(daemon, "_lock_all_unlocked",
                        lambda reason: called.append(reason) or ["work"])

    daemon._on_logind_prepare_for_sleep(True)
    daemon._on_logind_prepare_for_sleep(False)

    assert called == ["suspend"]


def test_logind_session_lock_locks_all(tmp_path, monkeypatch):
    daemon = _daemon(tmp_path)
    called = []
    monkeypatch.setattr(daemon, "_lock_all_unlocked",
                        lambda reason: called.append(reason) or [])

    daemon._on_logind_session_lock()

    assert called == ["screen-lock"]


def test_install_lifecycle_hooks_registers_logind_receivers(tmp_path):
    daemon = _daemon(tmp_path)

    class FakeManager:
        def GetSessionByPID(self, pid):
            assert isinstance(pid, int)
            return "/org/freedesktop/login1/session/_32"

    class FakeBus:
        def __init__(self):
            self.receivers = []

        def get_object(self, bus_name, path):
            assert bus_name == "org.freedesktop.login1"
            assert path == "/org/freedesktop/login1"
            return object()

        def add_signal_receiver(self, handler, **kwargs):
            self.receivers.append((handler, kwargs))

    bus = FakeBus()
    with patch("qdistro_pwd_daemon.dbus.Interface", return_value=FakeManager()):
        daemon._install_lifecycle_hooks(bus)

    assert [r[1]["signal_name"] for r in bus.receivers] == [
        "Lock",
        "PrepareForSleep",
    ]
    assert bus.receivers[0][1]["path"] == "/org/freedesktop/login1/session/_32"
