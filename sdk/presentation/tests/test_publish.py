"""Atomic publisher behaviour."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest
from qdistro_presentation.model import (
    SnapshotError,
    SnapshotPathError,
    example_snapshot,
    with_generation,
)
from qdistro_presentation.publish import write_disabled_envelope, write_snapshot


def test_atomic_replace_and_skip_unchanged(tmp_path: Path):
    snap = example_snapshot()
    result = write_snapshot(str(tmp_path), snap, require_unwritable_dirs=False)
    assert result.wrote is True
    path = tmp_path / "current.json"
    assert path.is_file()
    first = path.read_bytes()
    again = write_snapshot(str(tmp_path), snap, require_unwritable_dirs=False)
    assert again.wrote is False
    assert again.reason == "unchanged"
    assert path.read_bytes() == first


def test_does_not_remove_existing_on_failure(tmp_path: Path, monkeypatch):
    snap = example_snapshot()
    write_snapshot(str(tmp_path), snap, require_unwritable_dirs=False)
    original = (tmp_path / "current.json").read_bytes()

    def boom(*_args, **_kwargs):
        raise OSError("rename failed")

    monkeypatch.setattr(os, "rename", boom)
    with pytest.raises(OSError):
        write_snapshot(
            str(tmp_path),
            with_generation(
                type(snap)(
                    version=snap.version,
                    enabled=True,
                    generation="00000000-0000-0000-0000-000000000000",
                    mode="light",
                    colors=snap.colors,
                    fonts=snap.fonts,
                    metrics=snap.metrics,
                    motion=snap.motion,
                    tooltips_enabled=snap.tooltips_enabled,
                    icon_theme=snap.icon_theme,
                )
            ),
            require_unwritable_dirs=False,
            skip_unchanged=False,
        )
    assert (tmp_path / "current.json").read_bytes() == original
    leftovers = list(tmp_path.glob(".qdistro-presentation-*"))
    assert leftovers == []


def test_rejects_symlink_directory(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(SnapshotPathError):
        write_snapshot(str(link), example_snapshot(), require_unwritable_dirs=False)


def test_invalid_model_does_not_replace_existing(tmp_path: Path):
    snap = example_snapshot()
    write_snapshot(str(tmp_path), snap, require_unwritable_dirs=False)
    original = (tmp_path / "current.json").read_bytes()
    bad = replace(snap, version=2)
    with pytest.raises(SnapshotError):
        write_snapshot(str(tmp_path), bad, require_unwritable_dirs=False, skip_unchanged=False)
    assert (tmp_path / "current.json").read_bytes() == original


def test_disabled_envelope_changes_generation(tmp_path: Path):
    snap = example_snapshot()
    write_snapshot(str(tmp_path), snap, require_unwritable_dirs=False)
    disabled = write_disabled_envelope(str(tmp_path), snap, require_unwritable_dirs=False)
    assert disabled.wrote is True
    assert disabled.generation != snap.generation
