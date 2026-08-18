"""Broker startup ordering for the cold lineage-store path."""
from __future__ import annotations

import sqlite3

import pytest

pytest.importorskip("dbus")

import qdistro_admin_broker as B  # noqa: E402
import qdistro_lineage_store as lineage_store  # noqa: E402
from qdistro_admin_broker import Broker  # noqa: E402


def test_cold_lineage_store_is_ready_before_bus_name(monkeypatch, tmp_path):
    db_path = tmp_path / "lineage" / "export-lineage.sqlite"
    events = []

    class _ColdBroker:
        _get_lineage_store = Broker._get_lineage_store

        def __init__(self, bus):
            events.append("construct")
            self._lineage_store = None
            self._lineage_store_startup_error = None

    def acquire_name(name, bus, *, do_not_queue):
        events.append("name")
        assert db_path.is_file()
        with sqlite3.connect(db_path) as conn:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "receipts" in tables
        assert "lineage_chain" in tables
        return object()

    monkeypatch.setattr(B, "LINEAGE_DB_PATH", str(db_path))
    monkeypatch.setattr(B, "Broker", _ColdBroker)
    monkeypatch.setattr(B.dbus.service, "BusName", acquire_name)

    _name, broker = B._create_ready_broker(object())

    assert events == ["construct", "name"]
    assert broker._lineage_store.chain_head() == "qdistro-lineage-genesis"
    broker._lineage_store.close()


def test_lineage_init_failure_is_cached_before_bus_name(monkeypatch, tmp_path):
    events = []
    opens = []

    class _BrokenBroker:
        _get_lineage_store = Broker._get_lineage_store

        def __init__(self, bus):
            events.append("construct")
            self._lineage_store = None
            self._lineage_store_startup_error = None

    def acquire_name(*args, **kwargs):
        events.append("name")
        return object()

    def fail_open(path):
        events.append("lineage")
        opens.append(path)
        raise OSError("lineage disk unavailable")

    monkeypatch.setattr(B, "Broker", _BrokenBroker)
    monkeypatch.setattr(B, "LINEAGE_DB_PATH", str(tmp_path / "lineage.db"))
    monkeypatch.setattr(B.dbus.service, "BusName", acquire_name)
    monkeypatch.setattr(lineage_store, "LineageStore", fail_open)

    _name, broker = B._create_ready_broker(object())

    assert events == ["construct", "lineage", "name"]
    assert len(opens) == 1
    with pytest.raises(RuntimeError, match="unavailable since broker startup"):
        broker._get_lineage_store()
    assert len(opens) == 1, "cached startup failure must prevent lazy retry"


def test_cached_lineage_failure_returns_fast_dbus_error(monkeypatch):
    opens = []

    class _UnavailableBroker(Broker):
        def __init__(self):
            self._lineage_store = None
            self._lineage_store_startup_error = OSError(
                "lineage disk unavailable")

        def _require_root_lineage_peer(self, sender, conn, method):
            return 0, 1, "/usr/libexec/qdistro/qdistro_session_manager.py", 1

    def unexpected_open(path):
        opens.append(path)
        raise AssertionError("lineage store retried on D-Bus dispatch thread")

    monkeypatch.setattr(lineage_store, "LineageStore", unexpected_open)
    broker = _UnavailableBroker()

    with pytest.raises(B.dbus.DBusException) as exc:
        broker.GetLineageReceiptContext()

    assert exc.value.get_dbus_name() == B.BUS_NAME + ".LineageUnavailable"
    assert "unavailable since broker startup" in str(exc.value)
    assert opens == []
