"""Tests for the spec/19 Phase-8 broker SnapshotBefore + ListSnapshots
+ GetFiles surface.

Drives the methods in-process via the same _StubBroker harness used
by test_broker_check_permission.py + test_broker_save_rule.py. The
Snapper transport is replaced by a callable stub so the broker
talks to a local Python double, not a live SystemBus + snapperd.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("dbus")

import dbus  # noqa: E402

from test_broker_check_permission import (  # noqa: E402
    _StubBroker, ADMIN_UID, NON_ADMIN_UID,
)


@pytest.fixture
def broker(tmp_path: Path) -> _StubBroker:
    rd = tmp_path / "rules"
    rd.mkdir()
    b = _StubBroker(
        str(tmp_path / "approvals.sqlite"),
        str(tmp_path / "audit.sqlite"),
        str(rd),
    )
    return b


def _wire_fake_snapper(broker, calls: list, ret: dict):
    """Replace _snapper_client with a fake that records calls and
    returns canned replies keyed by method name."""
    from qdistro_snapshots import SnapperClient

    def transport(method, *args):
        calls.append((method, args))
        if method in ret:
            return ret[method]
        return []

    cli = SnapperClient(transport)
    broker._snapper_cached = cli
    return cli


# ---- SnapshotBefore (any caller) ---------------------------------

class TestSnapshotBefore:
    def test_returns_snapshot_number(self, broker):
        calls: list = []
        _wire_fake_snapper(broker, calls,
                           {"CreateSingleSnapshot": 17})
        broker.set_peer(uid=NON_ADMIN_UID)
        n = broker.SnapshotBefore("root", "before-test")
        assert int(n) == 17
        # The recorded transport call should include the
        # qdistro.origin userdata + caller_uid + caller_exe.
        assert calls
        method, args = calls[0]
        assert method == "CreateSingleSnapshot"
        # args = (config, description, cleanup, userdata)
        assert args[0] == "root"
        assert args[1] == "before-test"
        ud = args[3]
        assert ud["qdistro.origin"] == "1"
        assert ud["qdistro.caller_uid"] == str(NON_ADMIN_UID)
        assert "qdistro.caller_exe" in ud

    def test_rate_limited(self, broker, tmp_path):
        # Build a tight rate-limit broker so the second call rejects.
        rd = tmp_path / "rules2"
        rd.mkdir()
        b = _StubBroker(
            str(tmp_path / "approvals2.sqlite"),
            str(tmp_path / "audit2.sqlite"),
            str(rd),
            ratelimit_limit=1, ratelimit_window_s=10.0,
        )
        _wire_fake_snapper(b, [], {"CreateSingleSnapshot": 1})
        b.set_peer(uid=NON_ADMIN_UID)
        b.SnapshotBefore("root", "first")
        with pytest.raises(dbus.DBusException) as excinfo:
            b.SnapshotBefore("root", "second")
        assert "rate-limited" in str(excinfo.value).lower()


# ---- ListSnapshots (admin only) ----------------------------------

class TestListSnapshots:
    def test_admin_returns_normalised_rows(self, broker):
        # Snapper returns: (num, type, pre-num, date, uid, desc,
        #                    cleanup, userdata)
        raw = [
            (1, "single", 0, 1700_000_000.0, 0, "first", "number",
             {"qdistro.origin": "1", "qdistro.action": "before"}),
            (2, "pre", 0, 1700_000_001.5, 1000, "zypper-pre",
             "number", {}),
        ]
        _wire_fake_snapper(broker, [], {"ListSnapshots": raw})
        broker.set_peer(uid=ADMIN_UID)
        out = broker.ListSnapshots("root")
        assert len(out) == 2
        assert int(out[0]["num"]) == 1
        assert str(out[0]["description"]) == "first"
        assert bool(out[0]["qdistro_origin"]) is True
        assert bool(out[1]["qdistro_origin"]) is False

    def test_non_admin_denied(self, broker):
        _wire_fake_snapper(broker, [], {"ListSnapshots": []})
        broker.set_peer(uid=NON_ADMIN_UID)
        with pytest.raises(dbus.DBusException) as excinfo:
            broker.ListSnapshots("root")
        assert "AccessDenied" in str(excinfo.value)


# ---- GetFiles (admin only) ----------------------------------------

class TestGetFiles:
    def test_admin_normalises_files(self, broker):
        raw = [("/etc/foo", "M"), ("/etc/bar", "+")]
        _wire_fake_snapper(broker, [], {"GetFiles": raw})
        broker.set_peer(uid=ADMIN_UID)
        out = broker.GetFiles("root", 1, 2)
        assert len(out) == 2
        assert str(out[0]["path"]) == "/etc/foo"
        assert str(out[0]["status"]) == "M"

    def test_non_admin_denied(self, broker):
        _wire_fake_snapper(broker, [], {"GetFiles": []})
        broker.set_peer(uid=NON_ADMIN_UID)
        with pytest.raises(dbus.DBusException) as excinfo:
            broker.GetFiles("root", 1, 2)
        assert "AccessDenied" in str(excinfo.value)


# ---- SnapperUnavailable when client binding fails ----------------

class TestSnapperUnavailable:
    def test_list_surfaces_unavailable(self, broker, monkeypatch):
        # Force _snapper_client to raise the canonical error.
        def boom(self_):
            raise dbus.DBusException(
                "qdistro_snapshots module unavailable: forced",
                name="org.qdistro.AdminBroker1.SnapperUnavailable")
        monkeypatch.setattr(
            type(broker), "_snapper_client", boom)
        broker.set_peer(uid=ADMIN_UID)
        with pytest.raises(dbus.DBusException) as excinfo:
            broker.ListSnapshots("root")
        assert "SnapperUnavailable" in str(excinfo.value)
