"""On-host tests for lineage-receipt emission in disposable export-back
(Task 2). Drives the pure `qdistro_disposable_export.promote_export` with a
`receipt_ctx` + a real temp `LineageStore`, asserting the receipt SURFACES land
atomically beside the artifacts, seal into the store, and verify fail-closed.
The session-manager `import_from_disposable`/`_seal_export_receipts` wiring is
covered by the VM lane (it needs the OS admin user)."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

SM_DIR = Path(__file__).resolve().parents[2] / "session_manager"
if str(SM_DIR) not in sys.path:
    sys.path.insert(0, str(SM_DIR))

import qdistro_disposable_export as ex  # noqa: E402
import qdistro_lineage_receipts as lr  # noqa: E402
from qdistro_lineage_store import Entity, LineageStore  # noqa: E402

TOKEN = "abcdef0123456789abcdef0123456789"


def _meta(token=TOKEN, oc="agent-scratch", silo="work", inp=None):
    return {"version": 1, "launch_token": token, "request_silo": silo,
            "open_class": oc, "container": "disp-x", "created": 1,
            "input_basename": inp}


def _export(tmp_path, files: dict, *, with_ctx=True, chain_head="head-abc"):
    payload = tmp_path / "payload"
    payload.mkdir()
    for name, data in files.items():
        (payload / name).write_bytes(data)
    state = tmp_path / "state"
    state.mkdir()
    ctx = {"chain_head": chain_head, "issuer": "qdistro-session-manager"} \
        if with_ctx else None
    receipt = ex.promote_export(str(payload), str(state), meta=_meta(),
                                now_epoch=1_700_000_000, receipt_ctx=ctx)
    return receipt, Path(receipt["dest"])


def _seal(store, receipt):
    """Mirror session-manager `_seal_export_receipts` (the wiring under VM test)."""
    with store.transaction():
        for env in receipt.get("lineage_sidecars", []):
            store.record_entity(Entity(
                eid=env["entity"], kind="artifact", digest=env["artifact_digest"],
                locator=env.get("locator"), created_at=env["created_at"]))
            store.record_receipt(entity=env["entity"], kind="sidecar",
                                 locator=env.get("locator"),
                                 digest=env["artifact_digest"], payload=env)
        for child in (receipt.get("lineage_manifest") or {}).get("receipts", []):
            store.record_receipt(entity=child["entity"], kind="export-manifest",
                                 locator=child.get("locator"),
                                 digest=child["artifact_digest"], payload=child)


# --- surfaces land atomically ---------------------------------------------


def test_surfaces_written_beside_artifacts(tmp_path):
    receipt, dest = _export(tmp_path, {"result.txt": b"hello world",
                                       "data.bin": b"\x00\x01\x02"})
    # each artifact has a sidecar; the batch has a manifest
    assert (dest / "result.txt.qdistro-lineage.json").is_file()
    assert (dest / "data.bin.qdistro-lineage.json").is_file()
    assert (dest / "qdistro-export-manifest.json").is_file()
    # the returned envelopes are present for the caller to seal
    assert len(receipt["lineage_sidecars"]) == 2
    assert len(receipt["lineage_manifest"]["receipts"]) == 2


def test_no_ctx_emits_no_surfaces(tmp_path):
    receipt, dest = _export(tmp_path, {"a.txt": b"x"}, with_ctx=False)
    assert "lineage_sidecars" not in receipt
    assert not (dest / "a.txt.qdistro-lineage.json").exists()
    assert not (dest / "qdistro-export-manifest.json").exists()


def test_emission_failure_never_blocks_export(tmp_path, monkeypatch):
    # Lineage receipts are additive: if emission fails (e.g. the receipt library
    # is missing or a surface write errors), the export MUST still complete with
    # the artifacts landed and simply no receipts (the invariant codex flagged).
    def boom(*a, **k):
        raise ModuleNotFoundError("qdistro_lineage_receipts unavailable")

    monkeypatch.setattr(ex, "_emit_lineage_surfaces", boom)
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "result.txt").write_bytes(b"hello world")
    state = tmp_path / "state"
    state.mkdir()
    receipt = ex.promote_export(str(payload), str(state), meta=_meta(),
                                now_epoch=1_700_000_000,
                                receipt_ctx={"chain_head": "h", "issuer": "i"})
    dest = Path(receipt["dest"])
    assert (dest / "result.txt").read_bytes() == b"hello world"  # artifact landed
    assert "lineage_sidecars" not in receipt  # degraded to no receipts
    assert "lineage_manifest" not in receipt


def test_sidecar_envelope_binds_digest_and_full_token(tmp_path):
    receipt, dest = _export(tmp_path, {"result.txt": b"hello world"})
    env = lr.read_sidecar(str(dest / "result.txt"))
    assert env["entity"] == f"disp-export:{TOKEN}:result.txt"  # FULL token
    assert env["artifact_digest"] == hashlib.sha256(b"hello world").hexdigest()
    assert env["locator"] == "result.txt"  # relative, survives _place_at
    assert env["chain_head"] == "head-abc"


# --- seal + verify round-trip ---------------------------------------------


def test_sealed_sidecar_verifies_against_store(tmp_path):
    receipt, dest = _export(tmp_path, {"result.txt": b"hello world",
                                       "data.bin": b"\x00\x01"})
    store = LineageStore(str(tmp_path / "lineage.db"))
    try:
        _seal(store, receipt)
        for name in ("result.txt", "data.bin"):
            env = lr.read_sidecar(str(dest / name))
            assert lr.verify_against_store(store, env) is True
        # the on-disk manifest's children all verify too
        man = lr.read_export_manifest(str(dest))
        for child in man["receipts"]:
            assert lr.verify_against_store(store, child) is True
    finally:
        store.close()


def test_tampered_sidecar_fails_verify(tmp_path):
    receipt, dest = _export(tmp_path, {"result.txt": b"hello world"})
    store = LineageStore(str(tmp_path / "lineage.db"))
    try:
        _seal(store, receipt)
        side = dest / "result.txt.qdistro-lineage.json"
        env = json.loads(side.read_bytes())
        env["locator"] = "evil.txt"  # forge a field
        side.write_bytes(json.dumps(env).encode("utf-8"))
        tampered = lr.read_sidecar(str(dest / "result.txt"))
        assert lr.verify_against_store(store, tampered) is False
    finally:
        store.close()


def test_unsealed_surface_does_not_verify(tmp_path):
    # surfaces on disk but never sealed (the degraded path) -> fail closed.
    receipt, dest = _export(tmp_path, {"result.txt": b"hello world"})
    store = LineageStore(str(tmp_path / "lineage.db"))
    try:
        env = lr.read_sidecar(str(dest / "result.txt"))
        assert lr.verify_against_store(store, env) is False  # no sealed row
    finally:
        store.close()


# --- reserved-namespace collision defence ---------------------------------


@pytest.mark.parametrize("evil_name", ["qdistro-export-manifest.json",
                                       "report.qdistro-lineage.json"])
def test_payload_in_receipt_namespace_refused(tmp_path, evil_name):
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / evil_name).write_bytes(b"hostile")
    state = tmp_path / "state"
    state.mkdir()
    with pytest.raises(ex.ExportPolicyError):
        ex.promote_export(str(payload), str(state), meta=_meta(), now_epoch=1,
                          receipt_ctx={"chain_head": "h", "issuer": "i"})


def test_sanitize_filename_reserves_receipt_namespace():
    assert ex.sanitize_filename("qdistro-export-manifest.json") is None
    assert ex.sanitize_filename("x.qdistro-lineage.json") is None
    assert ex.sanitize_filename("normal.txt") == "normal.txt"


# --- empty export ----------------------------------------------------------


def test_empty_export_emits_empty_manifest(tmp_path):
    receipt, dest = _export(tmp_path, {})
    assert receipt["lineage_sidecars"] == []
    man = lr.read_export_manifest(str(dest))
    assert man["receipts"] == []
