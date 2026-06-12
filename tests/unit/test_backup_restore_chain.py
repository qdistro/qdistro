"""Unit coverage for the incremental-chain restore staging cleanup in
qdistro_backup_cli (06-backup-dr §6 finding F-A fix).

The functional restore path (real rage + signed manifests + btrfs send/receive)
is exercised by tests/integration/backup-e2e.bats (tar stub) and
tests/integration/vm/backup-btrfs-e2e.bats (real btrfs). These pure-Python tests
pin the SECURITY-critical helpers that route around real btrfs: the per-seq
ancestor staging dir is removed with a subvol-aware deleter that must NEVER
follow a symlink out of the staging tree when --dest is attacker-mediated
storage.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SNAP_DIR = REPO_ROOT / "snapshots"


def _load(label: str, path: Path):
    spec = importlib.util.spec_from_file_location(label, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[label] = mod
    spec.loader.exec_module(mod)
    return mod


_load("qdistro_backup_manifest", SNAP_DIR / "qdistro_backup_manifest.py")
bk = _load("qdistro_backup_cli", SNAP_DIR / "qdistro_backup_cli.py")

# A delete command that actually removes a (simulated) received subvol, standing
# in for "btrfs subvolume delete".
RM = ["rm", "-rf"]


class TestRealSubdirs:
    def test_excludes_symlinked_dirs_and_files(self, tmp_path):
        scan = tmp_path / "scan"
        scan.mkdir()
        (scan / "real").mkdir()
        (scan / "afile").write_text("x")
        target = tmp_path / "target"  # OUTSIDE the scanned dir
        target.mkdir()
        os.symlink(target, scan / "linkdir")  # symlink -> dir
        names = set(bk._real_subdirs(str(scan)))
        assert names == {"real"}  # real dir kept; symlink + file excluded

    def test_missing_path_is_empty(self, tmp_path):
        assert bk._real_subdirs(str(tmp_path / "nope")) == []


class TestPurgeChainDir:
    def _chain(self, dest: Path, n_seq: int) -> Path:
        chain = dest / ".qd-restore-chain"
        for i in range(n_seq):
            # each seq dir holds one "received subvol" (a real dir here)
            (chain / str(i) / "data").mkdir(parents=True)
        return chain

    def test_removes_all_staged_subvols_and_dir(self, tmp_path):
        dest = tmp_path / "dest"
        chain = self._chain(dest, 3)
        bk._purge_chain_dir(str(chain), RM)
        assert not chain.exists()

    def test_idempotent_on_absent_dir(self, tmp_path):
        bk._purge_chain_dir(str(tmp_path / "dest" / ".qd-restore-chain"), RM)

    def test_seq_level_symlink_is_not_followed(self, tmp_path):
        # An attacker who controls --dest plants a seq-level symlink to data
        # they want destroyed. Purge must unlink the LINK, never its target.
        dest = tmp_path / "dest"
        chain = dest / ".qd-restore-chain"
        chain.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "precious").write_text("keep me")
        os.symlink(outside, chain / "evil")
        bk._purge_chain_dir(str(chain), RM)
        assert not chain.exists()
        assert outside.is_dir()
        assert (outside / "precious").read_text() == "keep me"

    def test_child_level_symlink_is_not_followed(self, tmp_path):
        dest = tmp_path / "dest"
        seq0 = dest / ".qd-restore-chain" / "0"
        seq0.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "precious").write_text("keep me")
        os.symlink(outside, seq0 / "evil")  # child-level symlink
        bk._purge_chain_dir(str(dest / ".qd-restore-chain"), RM)
        assert not (dest / ".qd-restore-chain").exists()
        assert outside.is_dir()
        assert (outside / "precious").read_text() == "keep me"

    def test_symlinked_chain_dir_unlinks_link_not_target(self, tmp_path):
        dest = tmp_path / "dest"
        dest.mkdir()
        target = tmp_path / "target"
        target.mkdir()
        (target / "keep").write_text("data")
        link = dest / ".qd-restore-chain"
        os.symlink(target, link)
        bk._purge_chain_dir(str(link), RM)
        assert not link.is_symlink()
        assert not link.exists()
        assert target.is_dir()
        assert (target / "keep").read_text() == "data"


class TestDeleteReceived:
    def test_uses_delete_cmd_when_it_succeeds(self, tmp_path):
        d = tmp_path / "subvol"
        d.mkdir()
        bk._delete_received(RM, str(d))
        assert not d.exists()

    def test_falls_back_to_rmtree_when_cmd_fails(self, tmp_path):
        # "false" always exits non-zero (simulates btrfs absent / not a subvol);
        # the recursive-remove fallback must still clear the dir.
        d = tmp_path / "subvol"
        (d / "nested").mkdir(parents=True)
        bk._delete_received(["false"], str(d))
        assert not d.exists()
