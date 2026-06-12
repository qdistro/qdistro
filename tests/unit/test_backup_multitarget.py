"""Unit coverage for multi-target restore strictness in qdistro_backup_cli.

A restore may combine several --out-dir locations (an owner-side manifest store
plus a per-subvol remote blob layout). The combine path must FAIL CLOSED on
ambiguity rather than silently pick a copy: a seq present twice with different
bytes is a chain fork; a blob present twice with different bytes is a
substitution; manifests from two host_ids must never be merged.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SNAP_DIR = REPO_ROOT / "snapshots"


def _load(label, path):
    spec = importlib.util.spec_from_file_location(label, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[label] = mod
    spec.loader.exec_module(mod)
    return mod


mf = _load("qdistro_backup_manifest", SNAP_DIR / "qdistro_backup_manifest.py")
bk = _load("qdistro_backup_cli", SNAP_DIR / "qdistro_backup_cli.py")


def _blob(dir_path: Path, name: str, data: bytes) -> dict:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / name).write_bytes(data)
    return {"blob": name, "sha256": mf.sha256_hex(data), "size": len(data)}


def _write_manifest(dir_path: Path, seq: int, host_id: str, entries,
                    prev=None) -> bytes:
    dir_path.mkdir(parents=True, exist_ok=True)
    m = mf.build_manifest(seq=seq, host_id=host_id, created_at=1000 + seq,
                          entries=entries, prev_manifest_sha256=prev)
    canonical = mf.manifest_canonical_bytes(m)
    (dir_path / f"manifest-{seq}.json").write_bytes(canonical)
    return canonical


def _entry(subvol, name, data, parent=None):
    return mf.build_entry(subvol, name, mf.sha256_hex(data), len(data), parent)


class TestBlobIndex:
    def test_resolves_blobs_split_across_dirs(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        _blob(a, "data-0.btrfs.age", b"x")
        _blob(b, "data-1.btrfs.age", b"y")
        idx = bk._blob_index([str(a), str(b)])
        assert idx["data-0.btrfs.age"] == str(a)
        assert idx["data-1.btrfs.age"] == str(b)

    def test_identical_duplicate_blob_is_allowed(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        _blob(a, "data-0.btrfs.age", b"same")
        _blob(b, "data-0.btrfs.age", b"same")
        idx = bk._blob_index([str(a), str(b)])
        assert idx["data-0.btrfs.age"] in (str(a), str(b))

    def test_differing_duplicate_blob_is_fatal(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        _blob(a, "data-0.btrfs.age", b"one")
        _blob(b, "data-0.btrfs.age", b"TWO")
        with pytest.raises(bk.BackupError):
            bk._blob_index([str(a), str(b)])


class TestLoadChainMultiDir:
    def test_combines_manifests_across_dirs(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        c0 = _write_manifest(a, 0, "h", [_entry("data", "data-0.btrfs.age", b"x")])
        _write_manifest(b, 1, "h",
                        [_entry("data", "data-1.btrfs.age", b"y",
                                parent="data-0.btrfs.age")],
                        prev=mf.sha256_hex(c0))
        chain = bk._load_chain(mf, [str(a), str(b)], None, None, True)
        assert [m["seq"] for m in chain] == [0, 1]

    def test_identical_seq_duplicate_deduped(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        e = [_entry("data", "data-0.btrfs.age", b"x")]
        _write_manifest(a, 0, "h", e)
        _write_manifest(b, 0, "h", e)        # byte-identical copy in a 2nd dir
        chain = bk._load_chain(mf, [str(a), str(b)], None, None, True)
        assert [m["seq"] for m in chain] == [0]

    def test_conflicting_seq_is_fatal(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        _write_manifest(a, 0, "h", [_entry("data", "data-0.btrfs.age", b"x")])
        _write_manifest(b, 0, "h", [_entry("data", "data-0.btrfs.age", b"DIFFERENT")])
        with pytest.raises(bk.BackupError):
            bk._load_chain(mf, [str(a), str(b)], None, None, True)

    def test_mixed_host_ids_refused(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        c0 = _write_manifest(a, 0, "hostA", [_entry("data", "data-0.btrfs.age", b"x")])
        _write_manifest(b, 1, "hostB",
                        [_entry("data", "data-1.btrfs.age", b"y",
                                parent="data-0.btrfs.age")],
                        prev=mf.sha256_hex(c0))
        with pytest.raises(bk.BackupError):
            bk._load_chain(mf, [str(a), str(b)], None, None, True)
