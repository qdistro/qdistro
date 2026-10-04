"""Trusted path walks reject symlinks and writable leaves."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import qdistro_presentation.paths as paths_mod
from qdistro_presentation.model import SnapshotPathError, example_snapshot
from qdistro_presentation.paths import (
    DEPLOYMENT_META,
    ENV_OVERRIDE,
    DeploymentMeta,
    ResolvedPath,
    load_deployment_meta,
    read_snapshot_at,
    resolve_snapshot_path,
    walk_open,
)
from qdistro_presentation.publish import write_snapshot

_PACKAGE_ROOT = str(Path(__file__).resolve().parents[1])


def _bounded_python(
    source: str, *args: str, timeout: float = 2.0
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = _PACKAGE_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-c", source, *args],
        timeout=timeout,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


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
    # managed_dir must be absent, not defaulted: the default
    # /var/lib/qdistro/presentation EXISTS on a real qdistro install (the
    # image bakes it plus its deployment metadata), where resolving there
    # is the CORRECT polkit behaviour — the assertion under test is that the
    # override is ignored, and on an installed system that yields a managed
    # ResolvedPath, not None. Pinning an absent managed_dir keeps the None
    # expectation deterministic on installed and uninstalled hosts alike.
    absent = str(tmp_path / "no-managed-dir")
    assert resolve_snapshot_path(role="polkit", managed_dir=absent) is None
    assert resolve_snapshot_path(role="locker", managed_dir=absent) is None


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


def test_fifo_leaf_rejected_without_blocking(tmp_path: Path):
    fifo = tmp_path / "current.json"
    os.mkfifo(fifo, 0o600)
    source = (
        "import sys\n"
        "from qdistro_presentation.model import SnapshotPathError\n"
        "from qdistro_presentation.paths import walk_open\n"
        "try:\n"
        "    walk_open(sys.argv[1], require_unwritable_dirs=False)\n"
        "except SnapshotPathError as exc:\n"
        "    sys.exit(0 if 'regular file' in str(exc) else 2)\n"
        "sys.exit(3)\n"
    )
    proc = _bounded_python(source, str(fifo))
    assert proc.returncode == 0, proc.stderr


def test_fifo_read_snapshot_rejected_without_blocking(tmp_path: Path):
    fifo = tmp_path / "current.json"
    os.mkfifo(fifo, 0o600)
    source = (
        "import sys\n"
        "from qdistro_presentation.model import SnapshotPathError\n"
        "from qdistro_presentation.paths import read_snapshot_at\n"
        "try:\n"
        "    read_snapshot_at(sys.argv[1], kind='override', expected_uid=None)\n"
        "except SnapshotPathError as exc:\n"
        "    sys.exit(0 if 'regular file' in str(exc) else 2)\n"
        "sys.exit(3)\n"
    )
    proc = _bounded_python(source, str(fifo))
    assert proc.returncode == 0, proc.stderr


def test_forced_override_path_kind():
    resolved = ResolvedPath(
        path="/tmp/fixture.json", kind="override", expected_uid=None, watch=True
    )
    assert resolved.kind == "override"


def test_ordinary_existing_managed_dir_without_metadata_has_no_source(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    managed.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv(ENV_OVERRIDE, raising=False)
    monkeypatch.setenv("HOME", str(home))
    absent = str(tmp_path / "missing-deployment.json")

    def load_absent(path: str = DEPLOYMENT_META) -> DeploymentMeta | None:
        return load_deployment_meta(absent)

    monkeypatch.setattr(paths_mod, "load_deployment_meta", load_absent)
    assert (
        resolve_snapshot_path(role="ordinary", managed_dir=str(managed)) is None
    )


def test_ordinary_existing_managed_dir_uses_deployment_admin_uid(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.delenv(ENV_OVERRIDE, raising=False)
    monkeypatch.setattr(
        paths_mod,
        "load_deployment_meta",
        lambda path=DEPLOYMENT_META: DeploymentMeta(version=1, admin_uid=1000),
    )
    resolved = resolve_snapshot_path(role="ordinary", managed_dir=str(managed))
    assert resolved == ResolvedPath(
        path=str(managed) + "/current.json",
        kind="managed",
        expected_uid=1000,
        watch=True,
    )


def test_load_deployment_meta_walks_root_owned_ancestors(monkeypatch):
    seen: dict[str, object] = {}

    def fake_walk(
        path: str,
        *,
        ancestor_uids: dict[int, int] | None = None,
        leaf_uid: int | None = None,
        **kwargs: object,
    ) -> int:
        seen["path"] = path
        seen["ancestor_uids"] = ancestor_uids
        seen["leaf_uid"] = leaf_uid
        raise OSError("stop")

    monkeypatch.setattr(paths_mod, "walk_open", fake_walk)
    assert load_deployment_meta() is None
    assert seen["path"] == DEPLOYMENT_META
    assert seen["ancestor_uids"] == {0: 0, 1: 0, 2: 0, 3: 0}
    assert seen["leaf_uid"] == 0


def test_load_deployment_meta_user_owned_fixture_is_untrusted(tmp_path):
    meta = tmp_path / "deployment.json"
    meta.write_text('{"version":1,"admin_uid":1000}\n', encoding="utf-8")
    assert load_deployment_meta(str(meta)) is None
