"""Broker P02 handoff test: RelayMessage refuses when SessionManager
says the target silo isn't Active.

Builds on the _StubBroker pattern from test_broker_sendto.py — same
fixtures, only adds a `_silo_state` override.
"""
from __future__ import annotations

import threading
from pathlib import Path
from unittest import mock

import pytest

pytest.importorskip("dbus")

import qdistro_admin_broker as B  # noqa: E402
from qdistro_admin_broker import Broker  # noqa: E402
from qdistro_admin_cache import ApprovalCache  # noqa: E402
from qdistro_admin_audit import AuditLog  # noqa: E402
from qdistro_admin_ratelimit import RateLimiter  # noqa: E402
from qdistro_admin_rules import RulesEngine  # noqa: E402


ADMIN_UID = B.ADMIN_UID


class _StubBroker(Broker):
    def __init__(self, cache_db: str, audit_db: str, rules_dir: str,
                 silo_states: dict | None = None):
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending = {}
        self.cache = ApprovalCache(cache_db)
        self.audit = AuditLog(audit_db)
        self.rules = RulesEngine(rules_dir)
        self.ratelimit = RateLimiter(limit=10_000, window_s=1.0)
        self._audit_retention_days = 0
        self._peer_uid = ADMIN_UID
        self._peer_pid = 1
        self._peer_exe = "/usr/bin/test-harness"
        self._peer_start = 0
        self.pending_signals: list[int] = []
        self.decided_signals: list[tuple[int, str]] = []
        self.forwards: list[tuple[int, str, str, str]] = []
        self.forward_exc: BaseException | None = None
        # uid → state mapping the test wants the broker to "see" from
        # the session manager. None entries mean "manager has no row
        # for that uid" (legacy path; trust the caller).
        self.silo_states = dict(silo_states or {})

    def set_peer(self, uid: int, pid: int = 100,
                 exe: str = "/usr/bin/peer", start: int = 0) -> None:
        self._peer_uid = uid
        self._peer_pid = pid
        self._peer_exe = exe
        self._peer_start = start

    def _peer_info(self, sender, conn):
        return (self._peer_uid, self._peer_pid, self._peer_exe, self._peer_start)

    def _silo_state(self, target_uid: int) -> str | None:
        return self.silo_states.get(int(target_uid))

    def _relay_forward(self, target_uid, target_service, kind, payload):
        if self.forward_exc is not None:
            raise self.forward_exc
        self.forwards.append(
            (int(target_uid), str(target_service), str(kind), str(payload)))

    def RequestPending(self, rid):  # type: ignore[override]
        self.pending_signals.append(int(rid))

    def RequestDecided(self, rid, decision):  # type: ignore[override]
        self.decided_signals.append((int(rid), str(decision)))


@pytest.fixture
def broker_factory(tmp_path: Path):
    def _build(silo_states: dict | None = None):
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir(exist_ok=True)
        return _StubBroker(
            str(tmp_path / "approvals.sqlite"),
            str(tmp_path / "audit.sqlite"),
            str(rules_dir),
            silo_states=silo_states,
        )
    return _build


class _CB:
    def __init__(self):
        self.replies = []
        self.errors = []

    def reply(self, *args):
        self.replies.append(args)

    def error(self, exc):
        self.errors.append(exc)


class TestRelayGate:
    def test_refuses_when_target_silo_stopped(self, broker_factory):
        broker = broker_factory({3000: "Stopped"})
        broker.set_peer(uid=2000)
        cb = _CB()
        broker.RelayMessage(3000, "org.qdistro.StubNotepad",
                            "text/plain", "hi", cb.reply, cb.error)
        assert cb.replies == []
        assert len(cb.errors) == 1
        err = cb.errors[0]
        assert "SiloNotActive" in err.get_dbus_name()
        assert "Stopped" in str(err)

    def test_refuses_when_target_silo_frozen(self, broker_factory):
        broker = broker_factory({3000: "Frozen"})
        broker.set_peer(uid=2000)
        cb = _CB()
        broker.RelayMessage(3000, "org.qdistro.StubNotepad",
                            "text/plain", "hi", cb.reply, cb.error)
        assert len(cb.errors) == 1
        assert "SiloNotActive" in cb.errors[0].get_dbus_name()

    def test_allows_when_target_silo_active(self, broker_factory):
        broker = broker_factory({3000: "Active"})
        broker.set_peer(uid=2000)
        cb = _CB()
        broker.RelayMessage(3000, "org.qdistro.StubNotepad",
                            "text/plain", "hi", cb.reply, cb.error)
        # Enqueued for admin approval — no immediate error.
        assert len(cb.errors) == 0
        assert len(broker._pending) == 1

    def test_legacy_unknown_uid_falls_through(self, broker_factory):
        # silo_states is empty → _silo_state returns None → legacy
        # path: enqueue request for admin approval.
        broker = broker_factory({})
        broker.set_peer(uid=2000)
        cb = _CB()
        broker.RelayMessage(3000, "org.qdistro.StubNotepad",
                            "text/plain", "hi", cb.reply, cb.error)
        assert len(cb.errors) == 0
        assert len(broker._pending) == 1

    def test_unreachable_refuses_in_fail_closed_mode(self, broker_factory):
        # SEC-H1: when REQUIRE_SILO_ACTIVE is on, a session-manager
        # outage (modelled as _silo_state returning "Unreachable")
        # rejects the relay with SiloManagerUnreachable instead of
        # falling through to legacy trust.
        broker = broker_factory({3000: "Unreachable"})
        broker.set_peer(uid=2000)
        cb = _CB()
        broker.RelayMessage(3000, "org.qdistro.StubNotepad",
                            "text/plain", "hi", cb.reply, cb.error)
        assert cb.replies == []
        assert len(cb.errors) == 1
        err = cb.errors[0]
        assert "SiloManagerUnreachable" in err.get_dbus_name()


class TestRequireSiloActiveDefault:
    """The fail-closed default: an unset toggle means REQUIRE_SILO_ACTIVE
    is on, so a session-manager error yields the "Unreachable" sentinel
    rather than falling through to the legacy trust path.
    """

    def test_read_default_is_true(self):
        # No env var, no readable broker.conf → fail closed.
        with (mock.patch.dict("os.environ", {}, clear=True),
              mock.patch.object(B, "_BROKER_CONF_PATH",
                                "/nonexistent/qdistro/broker.conf")):
            assert B._read_require_silo_active() is True

    def test_read_env_false_overrides_to_fail_open(self):
        with (mock.patch.dict(
                "os.environ",
                {"QDISTRO_BROKER_REQUIRE_SILO_ACTIVE": "0"}),
              mock.patch.object(B, "_BROKER_CONF_PATH",
                                "/nonexistent/qdistro/broker.conf")):
            assert B._read_require_silo_active() is False

    def test_read_env_true(self):
        with mock.patch.dict(
                "os.environ",
                {"QDISTRO_BROKER_REQUIRE_SILO_ACTIVE": "1"}):
            assert B._read_require_silo_active() is True

    def test_read_conf_false_overrides_to_fail_open(self, tmp_path):
        conf = tmp_path / "broker.conf"
        conf.write_text("require_silo_active = false\n", encoding="utf-8")
        with (mock.patch.dict("os.environ", {}, clear=True),
              mock.patch.object(B, "_BROKER_CONF_PATH", str(conf))):
            assert B._read_require_silo_active() is False

    def test_read_env_invalid_value_fails_closed(self):
        # A typo / unrecognized env value must NOT silently fail open.
        for bad in ("ture", "2", "maybe", "enabled"):
            with (mock.patch.dict(
                    "os.environ",
                    {"QDISTRO_BROKER_REQUIRE_SILO_ACTIVE": bad}),
                  mock.patch.object(B, "_BROKER_CONF_PATH",
                                    "/nonexistent/qdistro/broker.conf")):
                assert B._read_require_silo_active() is True, bad

    def test_read_conf_invalid_value_fails_closed(self, tmp_path):
        # A typo in broker.conf must keep the gate closed, not open it.
        conf = tmp_path / "broker.conf"
        conf.write_text("require_silo_active = ture\n", encoding="utf-8")
        with (mock.patch.dict("os.environ", {}, clear=True),
              mock.patch.object(B, "_BROKER_CONF_PATH", str(conf))):
            assert B._read_require_silo_active() is True

    def test_silo_state_error_fails_closed_by_default(self, broker_factory):
        # Drive the REAL _silo_state (not the stub override) with the
        # default fail-closed posture: a session-manager DBus error must
        # map to the "Unreachable" sentinel, which RelayMessage rejects.
        broker = broker_factory()

        def _boom(*_a, **_k):
            raise B.dbus.DBusException(
                "no session manager",
                name="org.freedesktop.DBus.Error.ServiceUnknown")

        with (mock.patch.object(B, "REQUIRE_SILO_ACTIVE", True),
              mock.patch.object(B.dbus, "SystemBus", side_effect=_boom)):
            assert Broker._silo_state(broker, 3000) == "Unreachable"

    def test_silo_state_error_falls_through_when_disabled(self, broker_factory):
        # With the escape hatch engaged (REQUIRE_SILO_ACTIVE off), the
        # same error returns None → legacy trust fall-through.
        broker = broker_factory()

        def _boom(*_a, **_k):
            raise B.dbus.DBusException(
                "no session manager",
                name="org.freedesktop.DBus.Error.ServiceUnknown")

        with (mock.patch.object(B, "REQUIRE_SILO_ACTIVE", False),
              mock.patch.object(B.dbus, "SystemBus", side_effect=_boom)):
            assert Broker._silo_state(broker, 3000) is None
