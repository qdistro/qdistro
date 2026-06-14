"""Tests for broker-owned disposable export lineage recording."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

BROKER_DIR = Path(__file__).resolve().parents[2] / "broker"
if str(BROKER_DIR) not in sys.path:
    sys.path.insert(0, str(BROKER_DIR))

import qdistro_export_lineage as xl  # noqa: E402
import qdistro_lineage_receipts as lr  # noqa: E402
from qdistro_lineage_store import LineageStore  # noqa: E402


TOKEN = "abcdef0123456789abcdef0123456789"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _descriptor(tmp_path: Path, *, bad_digest: bool = False) -> dict:
    dest = tmp_path / "landing"
    dest.mkdir()
    files = []
    sidecars = []
    manifest_children = []
    for name, data in {"a.txt": b"alpha", "b.txt": b"beta"}.items():
        path = dest / name
        path.write_bytes(data)
        digest = _digest(data)
        recorded_digest = "0" * 64 if bad_digest and name == "b.txt" else digest
        entity = f"disp-export:{TOKEN}:{name}"
        side = lr.build_envelope(
            entity=entity,
            kind="sidecar",
            chain_head="head-before",
            created_at=1_700_000_000,
            artifact_digest=recorded_digest,
            locator=name,
            issuer="qdistro-broker",
        )
        man = lr.build_envelope(
            entity=entity,
            kind="export-manifest",
            chain_head="head-before",
            created_at=1_700_000_000,
            artifact_digest=recorded_digest,
            locator=name,
            issuer="qdistro-broker",
        )
        files.append({
            "entity": entity,
            "name": name,
            "path": str(path),
            "locator": name,
            "digest": recorded_digest,
            "size": len(data),
            "receipt": side,
        })
        sidecars.append(side)
        manifest_children.append(man)
    return {
        "version": 1,
        "launch_token": TOKEN,
        "request_silo": "work",
        "open_class": "agent-scratch",
        "mode": "export",
        "dest": str(dest),
        "source": {
            "kind": "disposable-export",
            "source_input": "input.txt",
            "locator": f"qdistro://disposable/{TOKEN}",
        },
        "files": files,
        "manifest": lr.build_export_manifest(
            manifest_children,
            chain_head="head-before",
            created_at=1_700_000_000,
            issuer="qdistro-broker",
        ),
    }


def test_record_export_activity_writes_structural_lineage_and_receipts(tmp_path):
    store = LineageStore(str(tmp_path / "lineage.db"))
    try:
        desc = xl.normalize_descriptor(_descriptor(tmp_path))
        xl.validate_landed_files(desc)
        result = xl.record_export_activity(store, desc, now=1_700_000_001)

        source = f"disposable-source:{TOKEN}"
        assert result.source == source
        assert set(store.downstream(source)) == set(result.outputs)
        for out in result.outputs:
            assert source in store.upstream(out)
            assertions = store.assertions_for(out, authority="broker-derived")
            assert any(
                a["fact"] == "security.snapshot.state"
                and a["value"] == "unresolved"
                and a["asserted_by"] == "broker"
                and a["authority"] == "broker-derived"
                for a in assertions
            )
            rows = store.receipts_for(out)
            assert {r["kind"] for r in rows} == {"sidecar", "export-manifest"}
            side = next(f.receipt for f in desc.files if f.entity == out)
            assert lr.verify_against_store(store, side) is True

        assert all(v == frozenset() for v in store.guarded_descendants(source).values())
        assert store.verify_chain(store.chain_head()) is True
    finally:
        store.close()


def test_record_export_activity_rejects_bad_digest_before_writing(tmp_path):
    store = LineageStore(str(tmp_path / "lineage.db"))
    try:
        desc = xl.normalize_descriptor(_descriptor(tmp_path, bad_digest=True))
        with pytest.raises(xl.ValidationFailed):
            xl.validate_landed_files(desc)
        assert store.chain_head() == "qdistro-lineage-genesis"
        assert store.downstream(f"disposable-source:{TOKEN}") == frozenset()
    finally:
        store.close()


def test_manifest_child_mismatch_rejects(tmp_path):
    raw = _descriptor(tmp_path)
    raw["manifest"] = json.loads(json.dumps(raw["manifest"]))
    raw["manifest"]["receipts"][0]["entity"] = "disp-export:evil:a.txt"
    desc = xl.normalize_descriptor(raw)
    with pytest.raises(xl.BadDescriptor):
        xl.validate_landed_files(desc)


def test_path_outside_dest_rejects(tmp_path):
    raw = _descriptor(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("alpha")
    raw["files"][0]["path"] = str(outside)
    desc = xl.normalize_descriptor(raw)
    with pytest.raises(xl.BadDescriptor):
        xl.validate_landed_files(desc)


def test_symlinked_intermediate_under_dest_rejects(tmp_path):
    raw = _descriptor(tmp_path)
    landing = Path(raw["dest"])
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.txt").write_bytes(b"alpha")
    (landing / "link").symlink_to(outside, target_is_directory=True)
    digest = _digest(b"alpha")
    raw["files"][0]["path"] = str(landing / "link" / "evil.txt")
    raw["files"][0]["locator"] = "link/evil.txt"
    raw["files"][0]["name"] = "evil.txt"
    raw["files"][0]["digest"] = digest
    raw["files"][0]["size"] = len(b"alpha")
    raw["files"][0]["receipt"] = lr.build_envelope(
        entity=raw["files"][0]["entity"],
        kind="sidecar",
        chain_head="head-before",
        created_at=1_700_000_000,
        artifact_digest=digest,
        locator="link/evil.txt",
        issuer="qdistro-broker",
    )
    raw["manifest"]["receipts"][0] = lr.build_envelope(
        entity=raw["files"][0]["entity"],
        kind="export-manifest",
        chain_head="head-before",
        created_at=1_700_000_000,
        artifact_digest=digest,
        locator="link/evil.txt",
        issuer="qdistro-broker",
    )
    desc = xl.normalize_descriptor(raw)
    with pytest.raises(xl.ValidationFailed):
        xl.validate_landed_files(desc)


def test_symlinked_dest_rejects(tmp_path):
    raw = _descriptor(tmp_path)
    actual = Path(raw["dest"])
    link = tmp_path / "dest-link"
    link.symlink_to(actual, target_is_directory=True)
    raw["dest"] = str(link)
    raw["files"][0]["path"] = str(link / "a.txt")
    raw["files"][1]["path"] = str(link / "b.txt")
    desc = xl.normalize_descriptor(raw)
    with pytest.raises(xl.ValidationFailed):
        xl.validate_landed_files(desc)


def test_fifo_leaf_rejects_without_blocking(tmp_path):
    raw = _descriptor(tmp_path)
    fifo = Path(raw["dest"]) / "fifo"
    fifo.unlink(missing_ok=True)
    os.mkfifo(fifo)
    raw["files"][0]["path"] = str(fifo)
    raw["files"][0]["locator"] = "fifo"
    raw["files"][0]["name"] = "fifo"
    raw["files"][0]["digest"] = "0" * 64
    raw["files"][0]["size"] = 0
    raw["files"][0]["receipt"] = lr.build_envelope(
        entity=raw["files"][0]["entity"],
        kind="sidecar",
        chain_head="head-before",
        created_at=1_700_000_000,
        artifact_digest="0" * 64,
        locator="fifo",
        issuer="qdistro-broker",
    )
    raw["manifest"]["receipts"][0] = lr.build_envelope(
        entity=raw["files"][0]["entity"],
        kind="export-manifest",
        chain_head="head-before",
        created_at=1_700_000_000,
        artifact_digest="0" * 64,
        locator="fifo",
        issuer="qdistro-broker",
    )
    desc = xl.normalize_descriptor(raw)
    with pytest.raises(xl.ValidationFailed):
        xl.validate_landed_files(desc)
