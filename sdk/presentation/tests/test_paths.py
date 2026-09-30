"""Trusted path walks reject symlinks and writable leaves."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from qdistro_presentation.model import SnapshotPathError, example_snapshot
from qdistro_presentation.paths import (
    ENV_OVERRIDE,
    ResolvedPath,
    read_snapshot_at,
    resolve_snapshot_path,
    walk_open,
)
from qdistro_presentation.publish import write_snapshot


def test_symlink_leaf_rejected(tmp_path: Path):
    target = tmp_path / "real.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "current.json"
    link.symlink_to(target)
    with pytest.raises(SnapshotPathError):
        walk_open(str(link), require_unwritable_dirs=False)


def test_override_malformed_does_not_probe_home(monkeypatch):
    monkeypatch.setenv(ENV_OVERRIDE, "relative/path.json")
    monkeypatch.setenv("HOME", "/tmp/someone-else")
    assert resolve_snapshot_path(role="ordinary") is None


def test_polkit_ignores_override(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "fixture.json"))
    assert resolve_snapshot_path(role="polkit") is None
    assert resolve_snapshot_path(role="locker") is None


def test_override_read_roundtrip(tmp_path: Path):
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    data, identity = read_snapshot_at(
        str(tmp_path / "current.json"),
        kind="override",
        expected_uid=None,
    )
    assert b'"version":1' in data
    assert identity[3] == len(data)


def test_writable_leaf_directory_rejected(tmp_path: Path):
    leaf = tmp_path / "presentation"
    leaf.mkdir()
    os.chmod(leaf, 0o777)
    with pytest.raises(SnapshotPathError, match="group/other-writable"):
        walk_open(str(leaf), leaf_directory=True, require_unwritable_dirs=True)


def test_sticky_world_writable_parent_of_file_rejected(tmp_path: Path):
    leaf = tmp_path / "presentation"
    leaf.mkdir()
    (leaf / "current.json").write_text("{}", encoding="utf-8")
    os.chmod(leaf, 0o1777)
    with pytest.raises(SnapshotPathError, match="group/other-writable"):
        walk_open(str(leaf / "current.json"), require_unwritable_dirs=True)


def test_forced_override_path_kind():
    resolved = ResolvedPath(
        path="/tmp/fixture.json", kind="override", expected_uid=None, watch=True
    )
    assert resolved.kind == "override"
