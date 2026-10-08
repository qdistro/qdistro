"""Atomic publisher behaviour."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
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


def test_skip_unchanged_fifo_does_not_block(tmp_path: Path):
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    path = tmp_path / "current.json"
    path.unlink()
    os.mkfifo(path, 0o600)
    source = (
        "import sys\n"
        "from qdistro_presentation.model import example_snapshot\n"
        "from qdistro_presentation.publish import write_snapshot\n"
        "result = write_snapshot(sys.argv[1], example_snapshot(), require_unwritable_dirs=False)\n"
        "sys.exit(0 if result.wrote else 2)\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get(
        "PYTHONPATH", ""
    )
    proc = subprocess.run(
        [sys.executable, "-c", source, str(tmp_path)],
        timeout=2,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert path.is_file()


def test_disabled_envelope_changes_generation(tmp_path: Path):
    snap = example_snapshot()
    write_snapshot(str(tmp_path), snap, require_unwritable_dirs=False)
    disabled = write_disabled_envelope(str(tmp_path), snap, require_unwritable_dirs=False)
    assert disabled.wrote is True
    assert disabled.generation != snap.generation


def test_standalone_write_does_not_load_deployment_metadata(tmp_path: Path, monkeypatch):
    def boom() -> None:
        raise AssertionError("standalone write must not load deployment metadata")

    monkeypatch.setattr("qdistro_presentation.publish.load_deployment_meta", boom)
    result = write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    assert result.wrote is True


def test_managed_write_requires_trusted_metadata(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("qdistro_presentation.publish.MANAGED_DIR", str(tmp_path))
    monkeypatch.setattr("qdistro_presentation.publish.load_deployment_meta", lambda: None)
    with pytest.raises(SnapshotPathError, match="trusted deployment metadata"):
        write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    assert not (tmp_path / "current.json").exists()


def test_managed_write_uses_metadata_admin_uid(tmp_path: Path, monkeypatch):
    from qdistro_presentation.paths import DeploymentMeta

    uid = os.getuid()
    monkeypatch.setattr("qdistro_presentation.publish.MANAGED_DIR", str(tmp_path))
    monkeypatch.setattr(
        "qdistro_presentation.publish.load_deployment_meta",
        lambda: DeploymentMeta(version=1, admin_uid=uid),
    )
    result = write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    assert result.wrote is True
    assert (tmp_path / "current.json").is_file()


def test_managed_write_replaces_matching_generation_with_wrong_owner(
    tmp_path: Path, monkeypatch
):
    from qdistro_presentation.paths import DeploymentMeta

    uid = os.getuid()
    monkeypatch.setattr("qdistro_presentation.publish.MANAGED_DIR", str(tmp_path))
    monkeypatch.setattr(
        "qdistro_presentation.publish.load_deployment_meta",
        lambda: DeploymentMeta(version=1, admin_uid=uid),
    )
    snap = example_snapshot()
    first = write_snapshot(str(tmp_path), snap, require_unwritable_dirs=False)
    assert first.wrote is True

    real_fstat = os.fstat

    def fake_fstat(fd: int) -> os.stat_result:
        info = real_fstat(fd)
        if stat.S_ISREG(info.st_mode):
            return os.stat_result(
                (
                    info.st_mode,
                    info.st_ino,
                    info.st_dev,
                    info.st_nlink,
                    uid + 1,
                    info.st_gid,
                    info.st_size,
                    info.st_atime,
                    info.st_mtime,
                    info.st_ctime,
                )
            )
        return info

    monkeypatch.setattr(os, "fstat", fake_fstat)
    again = write_snapshot(str(tmp_path), snap, require_unwritable_dirs=False)
    assert again.wrote is True
    assert again.reason == "replaced"


def test_managed_write_rejects_mismatched_owner_uid(tmp_path: Path, monkeypatch):
    from qdistro_presentation.paths import DeploymentMeta

    uid = os.getuid()
    monkeypatch.setattr("qdistro_presentation.publish.MANAGED_DIR", str(tmp_path))
    monkeypatch.setattr(
        "qdistro_presentation.publish.load_deployment_meta",
        lambda: DeploymentMeta(version=1, admin_uid=uid),
    )
    with pytest.raises(SnapshotPathError, match="does not match trusted"):
        write_snapshot(
            str(tmp_path),
            example_snapshot(),
            owner_uid=uid + 1,
            require_unwritable_dirs=False,
        )
    assert not (tmp_path / "current.json").exists()
