"""qdistro_pwd_fprint — fprintd verify wrapper tests.

The helper is a thin synchronous wrapper around fprintd's
``net.reactivated.Fprint.Device.VerifyStart`` cycle. We don't have a
real fingerprint reader on the dev box, so the tests stub the system
bus + the dbus.Interface returned by GetDefaultDevice and exercise the
state machine. No real fprintd needed.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

import qdistro_pwd_fprint as f


# ---------------------------------------------------------------------------
# admin_username
# ---------------------------------------------------------------------------

class TestAdminUsername:
    def test_resolves_existing_uid(self):
        # uid 0 → root on every Linux host.
        assert f.admin_username(0) == "root"

    def test_falls_back_to_jan(self):
        # 999_999 is unlikely to exist as a real user.
        assert f.admin_username(999_999) == "admin"


# ---------------------------------------------------------------------------
# is_fprintd_available
# ---------------------------------------------------------------------------

class _StubBus:
    """Minimal SystemBus stub. Methods raise on demand to mimic the
    "fprintd absent" / "no enrolled device" paths."""

    def __init__(self, *, fail_get_object=False, default_device="",
                 raise_dbus_on_default_device=False):
        self._fail_get_object = fail_get_object
        self._default_device = default_device
        self._raise_dbus_on_default_device = raise_dbus_on_default_device
        self.added_signals: list = []

    def get_object(self, bus_name, path):
        if self._fail_get_object:
            import dbus
            raise dbus.DBusException("no fprintd")
        return SimpleNamespace(_path=path)

    def add_signal_receiver(self, *args, **kwargs):
        match = SimpleNamespace(remove=lambda: None)
        self.added_signals.append(match)
        return match


class TestIsAvailable:
    def test_returns_false_when_no_default_device(self):
        bus = _StubBus(fail_get_object=True)
        assert f.is_fprintd_available(bus) is False

    def test_returns_true_when_path_present(self, monkeypatch):
        bus = _StubBus()

        class _Mgr:
            def GetDefaultDevice(self):
                return "/net/reactivated/Fprint/Device/0"

        # Patch dbus.Interface to return our stub manager regardless of
        # the underlying object.
        import dbus

        def fake_iface(_obj, _ifc):
            return _Mgr()

        monkeypatch.setattr(dbus, "Interface", fake_iface)
        assert f.is_fprintd_available(bus) is True

    def test_returns_false_when_default_device_raises(self, monkeypatch):
        bus = _StubBus()
        import dbus

        class _Mgr:
            def GetDefaultDevice(self):
                raise dbus.DBusException("no enrolled fingerprints")

        def fake_iface(_obj, _ifc):
            return _Mgr()

        monkeypatch.setattr(dbus, "Interface", fake_iface)
        assert f.is_fprintd_available(bus) is False


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

class _FakeDevice:
    """Drives a scripted (status, finished) sequence into the registered
    VerifyStatus signal handler. _FakeBus calls .pump() between Verify
    actions to deliver signals synchronously.
    """

    def __init__(self, sequence: list[tuple[str, bool]],
                 *, claim_raises=None, verify_start_raises=None):
        self.sequence = sequence
        self.claim_raises = claim_raises
        self.verify_start_raises = verify_start_raises
        self.handler = None

    def Claim(self, _user):
        if self.claim_raises:
            raise self.claim_raises

    def VerifyStart(self, _finger):
        if self.verify_start_raises:
            raise self.verify_start_raises
        # Fire all queued signals immediately so done.wait() returns.
        if self.handler is not None:
            for status, finished in self.sequence:
                self.handler(status, finished)

    def VerifyStop(self):
        return None

    def Release(self):
        return None


class _FakeManager:
    def __init__(self, device_path: str = "/net/reactivated/Fprint/Device/0"):
        self.device_path = device_path

    def GetDefaultDevice(self):
        return self.device_path


def _stub_dbus(monkeypatch, manager: _FakeManager, device: _FakeDevice):
    """Patch dbus.Interface so Manager / Device proxies return our stubs.
    Also make _StubBus.add_signal_receiver wire the device's handler."""
    import dbus

    def fake_iface(_obj, ifc):
        if str(ifc).endswith("Manager"):
            return manager
        return device

    monkeypatch.setattr(dbus, "Interface", fake_iface)
    bus = _StubBus()

    def add_signal(handler, **kwargs):
        device.handler = handler
        return SimpleNamespace(remove=lambda: None)

    bus.add_signal_receiver = add_signal  # type: ignore[method-assign]
    return bus


class TestVerify:
    def test_match_returns_true(self, monkeypatch):
        bus = _stub_dbus(monkeypatch,
                         _FakeManager(),
                         _FakeDevice([("verify-match", True)]))
        matched, reason = f.verify("admin", bus, timeout_s=1)
        assert matched is True
        assert reason == "fprint-match"

    def test_no_match_returns_false_with_status_reason(self, monkeypatch):
        bus = _stub_dbus(monkeypatch,
                         _FakeManager(),
                         _FakeDevice([("verify-no-match", True)]))
        matched, reason = f.verify("admin", bus, timeout_s=1)
        assert matched is False
        assert "verify-no-match" in reason

    def test_claim_failure_surfaces(self, monkeypatch):
        import dbus
        bus = _stub_dbus(
            monkeypatch, _FakeManager(),
            _FakeDevice([], claim_raises=dbus.DBusException("device busy")))
        matched, reason = f.verify("admin", bus, timeout_s=1)
        assert matched is False
        assert reason.startswith("fprintd-claim")

    def test_no_default_device(self, monkeypatch):
        import dbus

        class _NoDevice:
            def GetDefaultDevice(self):
                raise dbus.DBusException("no fprintd device")

        bus = _stub_dbus(monkeypatch, _NoDevice(), _FakeDevice([]))
        matched, reason = f.verify("admin", bus, timeout_s=1)
        assert matched is False
        assert "no-device" in reason

    def test_timeout_returns_false(self, monkeypatch):
        # Empty sequence + verify_start that does nothing → done.wait
        # times out. Use a 0-second timeout via monkeypatching the
        # default for fast test.
        bus = _stub_dbus(monkeypatch, _FakeManager(), _FakeDevice([]))
        matched, reason = f.verify("admin", bus, timeout_s=0)
        assert matched is False
        assert "timeout" in reason or "fprint:" in reason


# ---------------------------------------------------------------------------
# Integration: AutoUnlockPortalKeys gate
# ---------------------------------------------------------------------------

class TestAutoUnlockGate:
    """Pin the wiring on the daemon side: when REQUIRE_FPRINT is on, the
    daemon must call into the fprint helper before unsealing. Mocked at
    the helper boundary so we don't have to run a real bus."""

    @pytest.fixture
    def daemon_with_stash(self, tmp_path, monkeypatch):
        import qdistro_pwd_daemon as d
        import qdistro_pwd_pinstash as ps
        import qdistro_pwd_tpm as tpm
        from qdistro_pwd_audit import PwdAuditLog
        from qdistro_pwd_vault import create_vault

        vd = str(tmp_path / "vaults")
        audit_path = str(tmp_path / "audit.sqlite")
        stash_path = str(tmp_path / "portal-keys-pin.tpm")
        monkeypatch.setattr(d, "VAULT_DIR", vd)
        monkeypatch.setattr(d, "AUDIT_DB", audit_path)
        monkeypatch.setattr(d, "PORTAL_KEYS_VAULT", "portal-keys")
        monkeypatch.setattr(d, "PORTAL_PIN_STASH_PATH", stash_path)
        monkeypatch.setattr(ps, "DEFAULT_STASH_PATH", stash_path)
        monkeypatch.setattr(d, "select_backend",
                            lambda name=None: tpm.MockBackend())
        monkeypatch.setattr(d, "lookup_backend",
                            lambda name: tpm.MockBackend())
        create_vault(vd, "portal-keys", b"my-portal-pin")
        daemon = d.PwdDaemon.__new__(d.PwdDaemon)
        daemon._unlocked = {}
        daemon._audit = PwdAuditLog(audit_path)
        # Pre-stash the PIN so the unlock path is reachable.
        with patch.object(daemon, "_peer_info", return_value=(1000, 999)):
            daemon.StashPortalPin([int(b) for b in b"my-portal-pin"],
                                  sender=":1.42")
        return daemon, d

    def test_off_by_default_no_fprint_call(self, daemon_with_stash,
                                           monkeypatch):
        daemon, d = daemon_with_stash
        monkeypatch.setattr(d, "PORTAL_REQUIRE_FPRINT", False)
        # Sentinel: blow up if the fprint helper is invoked.
        monkeypatch.setattr(d, "fprint_verify",
                            lambda *a, **kw: pytest.fail("should not run"))
        monkeypatch.setattr(d, "fprint_is_available",
                            lambda *a, **kw: pytest.fail("should not run"))
        with patch.object(daemon, "VaultUnlocked", lambda *a, **kw: None):
            with patch.object(daemon, "_peer_info", return_value=(1000, 999)):
                ok = daemon.AutoUnlockPortalKeys(sender=":1.42")
        assert ok is True

    def test_on_match_unlocks(self, daemon_with_stash, monkeypatch):
        daemon, d = daemon_with_stash
        monkeypatch.setattr(d, "PORTAL_REQUIRE_FPRINT", True)
        monkeypatch.setattr(d, "fprint_is_available", lambda bus: True)
        monkeypatch.setattr(d, "fprint_verify",
                            lambda u, bus: (True, "fprint-match"))
        with patch.object(daemon, "VaultUnlocked", lambda *a, **kw: None):
            with patch.object(daemon, "_peer_info", return_value=(1000, 999)):
                ok = daemon.AutoUnlockPortalKeys(sender=":1.42")
        assert ok is True
        assert "portal-keys" in daemon._unlocked

    def test_on_mismatch_raises(self, daemon_with_stash, monkeypatch):
        daemon, d = daemon_with_stash
        monkeypatch.setattr(d, "PORTAL_REQUIRE_FPRINT", True)
        monkeypatch.setattr(d, "fprint_is_available", lambda bus: True)
        monkeypatch.setattr(d, "fprint_verify",
                            lambda u, bus: (False, "fprint:verify-no-match"))
        with patch.object(daemon, "VaultUnlocked", lambda *a, **kw: None):
            with patch.object(daemon, "_peer_info", return_value=(1000, 999)):
                with pytest.raises(d.PwdBadPassword):
                    daemon.AutoUnlockPortalKeys(sender=":1.42")
        # Vault remains locked.
        assert "portal-keys" not in daemon._unlocked

    def test_on_fprintd_unreachable_optional_skips(
            self, daemon_with_stash, monkeypatch):
        daemon, d = daemon_with_stash
        monkeypatch.setattr(d, "PORTAL_REQUIRE_FPRINT", True)
        monkeypatch.setattr(d, "PORTAL_FPRINT_OPTIONAL", True)
        monkeypatch.setattr(d, "fprint_is_available", lambda bus: False)
        monkeypatch.setattr(
            d, "fprint_verify",
            lambda *a, **kw: pytest.fail("should not call when unreachable"))
        with patch.object(daemon, "VaultUnlocked", lambda *a, **kw: None):
            with patch.object(daemon, "_peer_info", return_value=(1000, 999)):
                ok = daemon.AutoUnlockPortalKeys(sender=":1.42")
        assert ok is True
        assert "portal-keys" in daemon._unlocked

    def test_on_fprintd_unreachable_strict_raises(
            self, daemon_with_stash, monkeypatch):
        daemon, d = daemon_with_stash
        monkeypatch.setattr(d, "PORTAL_REQUIRE_FPRINT", True)
        monkeypatch.setattr(d, "PORTAL_FPRINT_OPTIONAL", False)
        monkeypatch.setattr(d, "fprint_is_available", lambda bus: False)
        with patch.object(daemon, "VaultUnlocked", lambda *a, **kw: None):
            with patch.object(daemon, "_peer_info", return_value=(1000, 999)):
                with pytest.raises(d.PwdPolicyError):
                    daemon.AutoUnlockPortalKeys(sender=":1.42")
        assert "portal-keys" not in daemon._unlocked
