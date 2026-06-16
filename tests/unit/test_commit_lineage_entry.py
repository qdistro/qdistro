"""Tests for the broker-owned git-commit lineage ENTRY POINT
(``qdistro_admin_broker.Broker.RecordCommitLineage`` + its strict
``_normalize_commit_lineage_descriptor`` re-validator).

The recording handler itself (``qdistro_commit_lineage.record_commit``) is
covered by ``test_commit_lineage.py``. THIS file pins the broker D-Bus surface
that the commit action enters through: it mirrors the sibling export-lineage
entry point (``RecordExportLineage``, commit 837e02e):

  * root-gate FIRST — a non-root caller can never obtain a trailer-appended
    (and thus signable) commit message,
  * the broker STRICTLY re-validates the descriptor shape (defense-in-depth on
    top of the handler) and never trusts caller-supplied guards/compartments,
  * the returned ``message`` carries a ``Qdistro-Lineage`` trailer that
    ``verify_against_store`` accepts (the bytes the caller then signs),
  * an unrecorded source (laundering attempt) and a guard-denied flow both
    fail closed with distinct, non-trailer-leaking D-Bus errors.

Model mirrors test_broker_check_permission.py: subclass Broker to bypass the
dbus.service.Object setup + GLib timers, then call methods directly.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import pytest

pytest.importorskip("dbus")  # harness needs real dbus-python

import dbus  # noqa: E402
import qdistro_admin_broker as B  # noqa: E402
import qdistro_lineage_receipts as lr  # noqa: E402
from qdistro_admin_broker import Broker  # noqa: E402
from qdistro_lineage_store import Entity, LineageStore  # noqa: E402

BUS_NAME = B.BUS_NAME


def _digest(data: bytes = b"tree") -> str:
    return hashlib.sha256(data).hexdigest()


class _StubBroker(Broker):
    """Minimal Broker that skips real dbus registration and routes the lineage
    store + peer lookup through in-test shims."""

    def __init__(self, store: LineageStore, *, peer_uid: int = 0):
        self._lock = threading.Lock()
        self._lineage_store = store
        self._peer_uid = peer_uid
        self._peer_pid = 100
        self._peer_exe = "/usr/lib/qdistro/qsu-commit"
        self._peer_start = 0

    def set_peer_uid(self, uid: int) -> None:
        self._peer_uid = uid

    def _peer_info(self, sender, conn):
        return (self._peer_uid, self._peer_pid, self._peer_exe,
                self._peer_start)

    def _get_lineage_store(self):  # type: ignore[override]
        return self._lineage_store


@pytest.fixture
def store(tmp_path: Path):
    s = LineageStore(str(tmp_path / "lineage.db"), now=lambda: 1000)
    yield s
    s.close()


@pytest.fixture
def broker(store):
    return _StubBroker(store)


def _commit_activities(store):
    """The recorded commit-create chokepoint verdicts (audit trail)."""
    return store._conn.execute(
        "SELECT verdict FROM activities WHERE chokepoint='commit-create'"
    ).fetchall()


def _record_source(store, eid="file:work-note", *, guards=frozenset(),
                   compartments=frozenset(), conflict_classes=frozenset()):
    """Record a source entity WITH its broker-derived security snapshot."""
    store.record_entity(Entity(
        eid=eid, kind="file", guards=guards, compartments=compartments,
        conflict_classes=conflict_classes, created_at=1000,
    ))


def _descriptor(**over) -> str:
    base = {
        "version": 1,
        "message": "lineage: wire commit chokepoint\n\nWhy body.",
        "sources": [{"eid": "file:work-note"}],
        "tree_digest": _digest(),
        "branch": "claude/topic",
        "agent_gid": "silo:work",
    }
    base.update(over)
    return json.dumps(base)


# --- happy path: signed-message trailer verifies against the store ----------

def test_record_commit_lineage_returns_verifiable_trailer(broker, store):
    _record_source(store)
    out = json.loads(broker.RecordCommitLineage(_descriptor()))
    assert out["lineage_sealed"] is True
    assert out["version"] == 1
    # The returned message is the bytes the caller signs; its trailer parses
    # back and verifies against the sealed central row (chain-anchored).
    parsed = lr.parse_git_trailer(out["message"])
    assert len(parsed) == 1
    assert lr.verify_against_store(store, parsed[0]) is True
    # The minted commit entity is derived from the recorded source.
    eid = out["commit_eid"]
    assert store.get_entity(eid) is not None
    assert "file:work-note" in store.upstream(eid)
    assert store.verify_chain(store.chain_head()) is True


def test_mints_commit_eid_when_omitted(broker, store):
    _record_source(store)
    out = json.loads(broker.RecordCommitLineage(_descriptor()))
    # No commit_eid was supplied; the broker minted one.
    assert out["commit_eid"].startswith("commit:")
    assert store.get_entity(out["commit_eid"]) is not None


# --- root gate: a non-root caller can never get a signable trailer ----------

def test_non_root_caller_denied_before_any_work(broker, store):
    _record_source(store)
    broker.set_peer_uid(2000)
    with pytest.raises(dbus.DBusException) as ei:
        broker.RecordCommitLineage(_descriptor())
    assert ei.value.get_dbus_name() == BUS_NAME + ".AccessDenied"
    # Fail-closed: the root gate fires BEFORE any work, so no commit-create
    # activity was even recorded (let alone a sealed trailer).
    assert _commit_activities(store) == []


def test_root_gate_fires_before_parsing_malformed_payload(broker, store):
    # The root gate must be checked BEFORE descriptor parsing: a non-root caller
    # sending garbage that WOULD otherwise be a .BadArgument must still see
    # .AccessDenied (never a parse error that confirms the method exists or does
    # work for them). Pins the "root gate first" ordering directly.
    broker.set_peer_uid(2000)
    with pytest.raises(dbus.DBusException) as ei:
        broker.RecordCommitLineage("not json {")
    assert ei.value.get_dbus_name() == BUS_NAME + ".AccessDenied"
    with pytest.raises(dbus.DBusException) as ei2:
        broker.RecordCommitLineage("")
    assert ei2.value.get_dbus_name() == BUS_NAME + ".AccessDenied"


# --- laundering guard: unrecorded source fails closed -----------------------

def test_unrecorded_source_fails_closed(broker, store):
    # Source is NEVER recorded in the store -> cannot be laundered into a clean
    # commit by naming it. BadCommitInput -> .BadArgument; no trailer leaked.
    with pytest.raises(dbus.DBusException) as ei:
        broker.RecordCommitLineage(_descriptor(sources=[{"eid": "file:ghost"}]))
    assert ei.value.get_dbus_name() == BUS_NAME + ".BadArgument"
    # No source was ever recorded; nothing was minted, chain stays at genesis.
    assert _commit_activities(store) == []
    assert store.chain_head() == "qdistro-lineage-genesis"


def test_duplicate_source_eids_rejected(broker, store):
    # record_commit rejects duplicate sources; pin the D-Bus mapping so a caller
    # cannot smuggle a source twice (shape/laundering edge). No trailer returned.
    _record_source(store, eid="file:dup")
    desc = _descriptor(sources=[{"eid": "file:dup"}, {"eid": "file:dup"}])
    with pytest.raises(dbus.DBusException) as ei:
        broker.RecordCommitLineage(desc)
    assert ei.value.get_dbus_name() == BUS_NAME + ".BadArgument"
    assert _commit_activities(store) == []


# --- fail-closed policy: cross-compartment commit denied, no trailer --------

def test_cross_compartment_commit_denied_no_trailer(broker, store):
    _record_source(
        store, eid="file:work-secret",
        guards=frozenset({"no-cross-contaminate"}),
        compartments=frozenset({"work"}),
        conflict_classes=frozenset({"home-work-separation"}),
    )
    desc = _descriptor(
        commit_eid="commit:denied",
        sources=[{"eid": "file:work-secret"}],
        dest_compartments=["home"],
        dest_conflict_classes=["home-work-separation"],
    )
    with pytest.raises(dbus.DBusException) as ei:
        broker.RecordCommitLineage(desc)
    # A fail-closed POLICY decision (distinct from infra) so the caller aborts
    # the commit rather than retrying; no trailer was handed back.
    assert ei.value.get_dbus_name() == BUS_NAME + ".LineagePolicyDenied"
    # The attempt is audited but no commit entity / receipt was minted, and the
    # chain stays verifiable (full broker observable fail-closed state).
    acts = store._conn.execute(
        "SELECT verdict FROM activities WHERE chokepoint='commit-create'"
    ).fetchall()
    assert acts == [("deny",)]
    assert store.get_entity("commit:denied") is None
    assert store.receipts_for("commit:denied") == []
    assert store.verify_chain(store.chain_head()) is True


# --- the broker never trusts caller-supplied security fields ----------------

def test_store_snapshot_wins_over_clean_descriptor(broker, store):
    # The source is recorded local-only in the store. The descriptor source
    # carries ONLY {eid} (no security fields are even accepted), so the only
    # snapshot is the store's -> it inherits onto the commit virally.
    _record_source(store, eid="file:guarded", guards=frozenset({"local-only"}))
    out = json.loads(broker.RecordCommitLineage(
        _descriptor(sources=[{"eid": "file:guarded"}])))
    assert "local-only" in store.get_entity(out["commit_eid"]).guards


def test_source_with_extra_security_keys_rejected(broker, store):
    # A caller cannot smuggle a clean endpoint for a source by inflating the
    # source object: only {eid} is permitted.
    _record_source(store, eid="file:guarded", guards=frozenset({"local-only"}))
    bad = _descriptor(sources=[{"eid": "file:guarded", "guards": []}])
    with pytest.raises(dbus.DBusException) as ei:
        broker.RecordCommitLineage(bad)
    assert ei.value.get_dbus_name() == BUS_NAME + ".BadArgument"


# --- strict descriptor re-validation (broker-side, pre-handler) -------------

@pytest.mark.parametrize("mutate", [
    {"version": 2},
    {"message": 123},
    {"sources": []},
    {"tree_digest": ""},
    {"sources": "nope"},
    {"tree_digest": "not-a-sha"},
    {"commit_eid": ""},
    {"branch": ""},
    {"dest_compartments": [""]},
    {"dest_conflict_classes": "home"},
])
def test_malformed_descriptor_rejected(broker, store, mutate):
    _record_source(store)
    with pytest.raises(dbus.DBusException) as ei:
        broker.RecordCommitLineage(_descriptor(**mutate))
    assert ei.value.get_dbus_name() == BUS_NAME + ".BadArgument"
    # A bad shape is rejected pre-handler -> no commit-create activity minted.
    assert _commit_activities(store) == []


def test_unknown_top_level_key_rejected(broker, store):
    _record_source(store)
    with pytest.raises(dbus.DBusException) as ei:
        broker.RecordCommitLineage(_descriptor(sneaky="x"))
    assert ei.value.get_dbus_name() == BUS_NAME + ".BadArgument"


def test_non_json_descriptor_rejected(broker, store):
    with pytest.raises(dbus.DBusException) as ei:
        broker.RecordCommitLineage("not json {")
    assert ei.value.get_dbus_name() == BUS_NAME + ".BadArgument"


def test_empty_descriptor_rejected(broker, store):
    with pytest.raises(dbus.DBusException) as ei:
        broker.RecordCommitLineage("")
    assert ei.value.get_dbus_name() == BUS_NAME + ".BadArgument"
