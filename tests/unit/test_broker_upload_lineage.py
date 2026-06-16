"""Tests for the AdminBroker1 upload-lineage entry point
(qdistro_admin_broker.Broker.RecordUploadLineage) and its descriptor parser
(qdistro_upload_lineage_entry).

The entry point is the root-gated broker surface that calls the upload
chokepoint. It is ROOT-ONLY (a non-root silo could otherwise mint a sealed
upload receipt for any tracked source it can name, since the store has no
per-silo source-ownership model yet). The broker derives the uploading agent
from the authenticated peer uid (never the JSON body), reads the source security
snapshot store-authoritatively, and fails closed on an untracked / guarded
source.

Model mirrors test_broker_check_permission.py: subclass Broker to bypass the
dbus.service.Object setup, override _peer_info + _get_lineage_store, then call
the method directly.
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
from pathlib import Path

import pytest

pytest.importorskip("dbus")  # harness needs real dbus-python
import dbus  # noqa: E402

BROKER_DIR = Path(__file__).resolve().parents[2] / "broker"
if str(BROKER_DIR) not in sys.path:
    sys.path.insert(0, str(BROKER_DIR))

import qdistro_admin_broker as B  # noqa: E402
import qdistro_upload_lineage_entry as ue  # noqa: E402
from qdistro_admin_broker import Broker  # noqa: E402
from qdistro_lineage_receipts import verify_against_store  # noqa: E402
from qdistro_lineage_store import Entity, LineageStore  # noqa: E402

ROOT_UID = 0
NON_ADMIN_UID = 2000
DEST = "https://example.com/upload"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _StubBroker(Broker):
    def __init__(self, store: LineageStore):
        self._lock = threading.Lock()
        self._lineage_store = store
        # Root by default: the entry point is root-gated.
        self._peer_uid = ROOT_UID

    def set_peer(self, uid: int) -> None:
        self._peer_uid = uid

    def _peer_info(self, sender, conn):
        return (self._peer_uid, 100, "/usr/bin/qdistro-browser-bridge", 0)

    def _get_lineage_store(self):
        return self._lineage_store


@pytest.fixture
def store(tmp_path):
    s = LineageStore(str(tmp_path / "lineage.db"), now=lambda: 1000)
    yield s
    s.close()


@pytest.fixture
def broker(store):
    return _StubBroker(store)


def _record_source(store, eid, data, **kw):
    store.record_entity(Entity(eid=eid, kind="file", created_at=1000, **kw))


def _descriptor(files, *, destination=DEST, version=1):
    return json.dumps({
        "version": version,
        "destination": destination,
        "files": files,
    })


# ---- descriptor parser ------------------------------------------------------

def test_parser_accepts_minimal_descriptor():
    desc = ue.load_descriptor_json(_descriptor(
        [{"source_eid": "file:a", "digest": _digest(b"a")}]))
    assert desc.destination == DEST
    assert desc.files[0].source_eid == "file:a"
    assert desc.files[0].locator is None


def test_parser_rejects_authority_field_top():
    # A caller must not assert its own agent/silo identity. The whitelist
    # rejects it as an unknown top-level key.
    payload = json.dumps({
        "version": 1, "destination": DEST, "agent_gid": "silo:root",
        "files": [{"source_eid": "file:a", "digest": _digest(b"a")}],
    })
    with pytest.raises(ue.UploadDescriptorError):
        ue.load_descriptor_json(payload)


def test_parser_rejects_security_snapshot_per_file():
    # A caller must not assert a (clean) source security snapshot.
    f = {"source_eid": "file:a", "digest": _digest(b"a"),
         "guards": [], "compartments": []}
    with pytest.raises(ue.UploadDescriptorError):
        ue.load_descriptor_json(_descriptor([f]))


def test_parser_rejects_unknown_top_level_key():
    # Strict whitelist: an arbitrary unlisted key (a typo or an authority field
    # smuggled under a name the denylist never enumerated) is refused, not
    # silently dropped.
    payload = json.dumps({
        "version": 1, "destination": DEST, "extra": "ignored",
        "files": [{"source_eid": "file:a", "digest": _digest(b"a")}],
    })
    with pytest.raises(ue.UploadDescriptorError):
        ue.load_descriptor_json(payload)


def test_parser_rejects_unknown_per_file_key_with_nested_authority():
    # A nested authority-looking object under an unlisted per-file key must be
    # refused, not discarded.
    f = {"source_eid": "file:a", "digest": _digest(b"a"),
         "metadata": {"guards": ["local-only"]}}
    with pytest.raises(ue.UploadDescriptorError):
        ue.load_descriptor_json(_descriptor([f]))


def test_parser_rejects_duplicate_object_keys():
    # A shadowed key must not slip a value past the whitelist (last-wins).
    payload = ('{"version": 1, "version": 1, "destination": "%s", '
               '"files": [{"source_eid": "file:a", "digest": "%s"}]}'
               % (DEST, _digest(b"a")))
    with pytest.raises(ue.UploadDescriptorError):
        ue.load_descriptor_json(payload)


def test_parser_accepts_locator():
    desc = ue.load_descriptor_json(_descriptor(
        [{"source_eid": "file:a", "digest": _digest(b"a"),
          "locator": "https://x/a"}]))
    assert desc.files[0].locator == "https://x/a"


def test_parser_rejects_bad_digest():
    with pytest.raises(ue.UploadDescriptorError):
        ue.load_descriptor_json(_descriptor(
            [{"source_eid": "file:a", "digest": "nope"}]))


def test_parser_rejects_empty_files():
    with pytest.raises(ue.UploadDescriptorError):
        ue.load_descriptor_json(_descriptor([]))


def test_parser_rejects_bad_version():
    with pytest.raises(ue.UploadDescriptorError):
        ue.load_descriptor_json(_descriptor(
            [{"source_eid": "file:a", "digest": _digest(b"a")}], version=2))


# ---- broker method ----------------------------------------------------------

def test_tracked_clean_source_seals_receipt(broker, store):
    _record_source(store, "file:a", b"alpha")
    out = broker.RecordUploadLineage(_descriptor(
        [{"source_eid": "file:a", "digest": _digest(b"alpha")}]))
    res = json.loads(out)
    assert res["lineage_sealed"] is True
    man = res["manifest"]
    assert man["destination"] == DEST
    assert len(man["receipts"]) == 1
    child = man["receipts"][0]
    assert child["extra"]["destination"] == DEST
    assert verify_against_store(store, child) is True
    assert store.verify_chain(store.chain_head()) is True


def test_non_root_caller_rejected(broker, store):
    # Root-gated: a non-root silo cannot reach the upload chokepoint (it could
    # otherwise mint a sealed receipt for any tracked source it can name).
    _record_source(store, "file:a", b"alpha")
    broker.set_peer(NON_ADMIN_UID)
    with pytest.raises(dbus.DBusException) as ei:
        broker.RecordUploadLineage(_descriptor(
            [{"source_eid": "file:a", "digest": _digest(b"alpha")}]))
    assert ei.value.get_dbus_name().endswith("AccessDenied")
    # The gate fires before any store work: nothing sealed.
    n = store._conn.execute(
        "SELECT COUNT(*) FROM receipts WHERE kind='upload-receipt'").fetchone()
    assert n[0] == 0


def test_agent_gid_comes_from_peer_uid_not_body(broker, store):
    # The recorded uploading agent must be derived from the authenticated peer,
    # never a body field. With the root gate, the peer is root → silo:<root>.
    _record_source(store, "file:a", b"alpha")
    expected = f"silo:{B._username_for_uid(ROOT_UID)}"
    out = json.loads(broker.RecordUploadLineage(_descriptor(
        [{"source_eid": "file:a", "digest": _digest(b"alpha")}])))
    out_eid = out["outputs"][0]
    rows = store._conn.execute(
        "SELECT object FROM edges WHERE predicate='wasAttributedTo' "
        "AND subject=?", (out_eid,)).fetchall()
    assert (expected,) in rows


def test_untracked_source_fails_closed(broker, store):
    # The laundering guard: a source the store never recorded cannot be
    # uploaded with a sealed receipt by merely naming it.
    with pytest.raises(dbus.DBusException) as ei:
        broker.RecordUploadLineage(_descriptor(
            [{"source_eid": "file:ghost", "digest": _digest(b"x")}]))
    assert ei.value.get_dbus_name().endswith("LineageValidationFailed")
    n = store._conn.execute(
        "SELECT COUNT(*) FROM receipts WHERE kind='upload-receipt'").fetchone()
    assert n[0] == 0


def test_local_only_source_denied_no_receipt(broker, store):
    _record_source(store, "file:secret", b"beta",
                   guards=frozenset({"local-only"}))
    with pytest.raises(dbus.DBusException) as ei:
        broker.RecordUploadLineage(_descriptor(
            [{"source_eid": "file:secret", "digest": _digest(b"beta")}]))
    assert ei.value.get_dbus_name().endswith("LineagePolicyDenied")
    n = store._conn.execute(
        "SELECT COUNT(*) FROM receipts WHERE kind='upload-receipt'").fetchone()
    assert n[0] == 0


def test_mixed_batch_refuses_whole_batch(broker, store):
    _record_source(store, "file:clean", b"alpha")
    _record_source(store, "file:secret", b"beta",
                   guards=frozenset({"local-only"}))
    with pytest.raises(dbus.DBusException) as ei:
        broker.RecordUploadLineage(_descriptor([
            {"source_eid": "file:clean", "digest": _digest(b"alpha")},
            {"source_eid": "file:secret", "digest": _digest(b"beta")},
        ]))
    assert ei.value.get_dbus_name().endswith("LineagePolicyDenied")
    # Even the clean member gets no sealed receipt (batch fails closed).
    assert store.receipts_for("file:clean") == []


def test_caller_security_fields_cannot_clean_a_local_only_source(broker, store):
    # The source is local-only in the STORE; a body that tries to assert clean
    # guards is rejected by the parser before the store snapshot is even read.
    _record_source(store, "file:secret", b"beta",
                   guards=frozenset({"local-only"}))
    f = {"source_eid": "file:secret", "digest": _digest(b"beta"), "guards": []}
    with pytest.raises(dbus.DBusException) as ei:
        broker.RecordUploadLineage(_descriptor([f]))
    assert ei.value.get_dbus_name().endswith("BadArgument")


def test_malformed_json_rejected(broker):
    with pytest.raises(dbus.DBusException) as ei:
        broker.RecordUploadLineage("{not json")
    assert ei.value.get_dbus_name().endswith("BadArgument")
