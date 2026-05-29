"""Tests for broker RegisterLaunch (permission-lineage Phase 1).

RegisterLaunch is root-only and re-verifies the registered pid against
/proc before storing. We bypass dbus registration with a stub and
monkeypatch the module-level /proc readers the method calls.
"""
from __future__ import annotations

import threading

import pytest

pytest.importorskip("dbus")
import dbus  # noqa: E402

import qdistro_admin_broker as B  # noqa: E402
from qdistro_admin_broker import Broker  # noqa: E402
from qdistro_admin_audit import AuditLog  # noqa: E402
from qdistro_launch_record import LaunchRecordStore  # noqa: E402


class _StubBroker(Broker):
    def __init__(self, audit_db: str, *, launcher_uid: int):
        self._lock = threading.Lock()
        self.audit = AuditLog(audit_db)
        self.launch_records = LaunchRecordStore()
        self._launcher_uid = launcher_uid

    def _peer_info(self, sender, conn):
        return (self._launcher_uid, 9999, "/usr/libexec/qdistro/launcher", 0)


@pytest.fixture
def fake_target(monkeypatch):
    state = {"exe": "/usr/bin/firefox", "starttime": 777, "uid": 2000,
             "label": "u:r:qdistro_tier1_t:s0", "cgroup": "/user.slice/app"}
    monkeypatch.setattr(B, "_read_proc_identity",
                        lambda pid: (state["exe"], state["starttime"]))
    monkeypatch.setattr(B, "_read_proc_uid", lambda pid: state["uid"])
    monkeypatch.setattr(B, "_read_proc_selinux_label",
                        lambda pid: state["label"])
    monkeypatch.setattr(B._pi, "read_cgroup", lambda pid: state["cgroup"])
    return state


@pytest.fixture
def broker(tmp_path):
    return _StubBroker(str(tmp_path / "audit.sqlite"), launcher_uid=0)


def _call(broker, *, silo="work", engine="qdistro.tier1",
          app_id="qdistro.tier1.work", instance="i1",
          exe="/usr/bin/firefox", pid=1234, ns="", starttime=0):
    return broker.RegisterLaunch(silo, engine, app_id, instance, exe,
                                 pid, ns, starttime)


class TestAuthorization:
    def test_non_root_denied(self, tmp_path, fake_target):
        b = _StubBroker(str(tmp_path / "a.sqlite"), launcher_uid=1000)
        with pytest.raises(dbus.DBusException) as ei:
            _call(b)
        assert "AccessDenied" in str(ei.value.get_dbus_name())

    def test_root_allowed(self, broker, fake_target):
        rid = _call(broker)
        assert rid
        rec = broker.launch_records.get(rid)
        assert rec.silo == "work"
        assert rec.sandbox_engine == "qdistro.tier1"
        assert rec.uid == 2000


class TestReverification:
    def test_dead_target_rejected(self, broker, fake_target):
        fake_target["starttime"] = 0  # process gone
        with pytest.raises(dbus.DBusException) as ei:
            _call(broker)
        assert "CallerGone" in str(ei.value.get_dbus_name())

    def test_starttime_mismatch_rejected(self, broker, fake_target):
        # Launcher claims starttime 111 but live is 777.
        with pytest.raises(dbus.DBusException) as ei:
            _call(broker, starttime=111)
        assert "Mismatch" in str(ei.value.get_dbus_name())

    def test_exe_mismatch_rejected(self, broker, fake_target):
        with pytest.raises(dbus.DBusException) as ei:
            _call(broker, exe="/usr/bin/somethingelse")
        assert "Mismatch" in str(ei.value.get_dbus_name())

    def test_invalid_pid_rejected(self, broker, fake_target):
        with pytest.raises(dbus.DBusException) as ei:
            _call(broker, pid=0)
        assert "BadArgument" in str(ei.value.get_dbus_name())

    def test_unreadable_live_exe_rejected(self, broker, fake_target):
        # The broker is root; an unreadable live exe means we can't anchor
        # the record on a real exe → refuse to mint it (fail closed).
        fake_target["exe"] = "?"
        with pytest.raises(dbus.DBusException) as ei:
            _call(broker)
        assert "Mismatch" in str(ei.value.get_dbus_name())

    def test_stored_record_uses_live_facts(self, broker, fake_target):
        # Launcher's claimed label is ignored in favour of the live one.
        rid = _call(broker)
        rec = broker.launch_records.get(rid)
        assert rec.selinux_label == "u:r:qdistro_tier1_t:s0"
        assert rec.cgroup == "/user.slice/app"
        assert rec.starttime == 777
