"""Unit coverage for the verify-only restore REHEARSAL (06-backup-dr §3.3).

The full sig+chain+blob rehearsal over REAL rage + a real signed chain is the
host bats lane tests/integration/backup-rehearse-e2e.bats (and the real-btrfs
receive half is the VM lane). These pure-Python tests pin the pieces a bats lane
reads less precisely and the FALSE-GREEN guards that must fail loudly:

  - rehearsal pulls the chain from the REMOTE (read-only), never the local store;
  - it REFUSES when signature material (allowed_signers/sign_identity) is absent;
  - freshness: a remote behind the local state.json anchor FAILS;
  - an empty remote FAILS;
  - argparse exposes `rehearse` with a read-only default + opt-in receive.
"""
from __future__ import annotations

import importlib.util
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
_load("qdistro_backup_cli", SNAP_DIR / "qdistro_backup_cli.py")
svc = _load("qdistro_backup_service", SNAP_DIR / "qdistro_backup_service.py")


def _conf(tmp_path, remote, *, signed=True, state_seq=None):
    body = [
        'host_id = "h1"',
        'recipients = "/r"',
        f'remote = "{remote}"',
        'sign_key = "/k"',
        f'state_dir = "{tmp_path / "state"}"',
    ]
    if signed:
        body += ['allowed_signers = "/as"', 'sign_identity = "owner@x"']
    body += ['[[subvol]]', 'name = "data"', 'source = "/d"']
    p = tmp_path / "backup.conf"
    p.write_text("\n".join(body) + "\n")
    if state_seq is not None:
        st = svc.State(str(tmp_path / "state"), "h1")
        st.load()
        st.commit(state_seq, {})
    return str(p)


def _args(config, **kw):
    from types import SimpleNamespace
    base = dict(config=config, rsync_cmd="rsync -aHAX", ssh_cmd="ssh",
                rehearse_receive=False, rehearse_subvol=None,
                identity_file=None, receive_cmd="btrfs receive",
                snapshot_delete_cmd="btrfs subvolume delete")
    base.update(kw)
    return SimpleNamespace(**base)


class TestRefusals:
    def test_refuses_without_signature_material(self, tmp_path, capsys):
        remote = tmp_path / "remote"
        remote.mkdir()
        config = _conf(tmp_path, str(remote), signed=False)
        rc = svc.cmd_rehearse(_args(config))
        assert rc == 1
        assert "signature verification" in capsys.readouterr().err

    def test_empty_remote_fails(self, tmp_path, capsys):
        remote = tmp_path / "remote"
        remote.mkdir()
        config = _conf(tmp_path, str(remote), signed=True)
        rc = svc.cmd_rehearse(_args(config))
        assert rc == 1
        assert "no manifests" in capsys.readouterr().err


class TestPullRemoteChain:
    def test_pulls_only_manifests_and_sigs_read_only(self, tmp_path):
        remote = tmp_path / "remote"
        remote.mkdir()
        (remote / "manifest-0.json").write_text("{}")
        (remote / "manifest-0.json.sig").write_text("sig")
        (remote / "manifest-2.json").write_text("{}")
        (remote / "data-0.btrfs.age").write_bytes(b"blob")
        (remote / "junk.txt").write_text("x")
        target = svc.make_target(str(remote), "rsync", "ssh")
        work = tmp_path / "work"
        work.mkdir()
        seqs = svc._pull_remote_chain(target, str(work))
        assert seqs == [0, 2]
        pulled = sorted(p.name for p in work.iterdir())
        # manifests + the present sig pulled; blob/junk NOT (blobs pulled later)
        assert pulled == ["manifest-0.json", "manifest-0.json.sig",
                          "manifest-2.json"]
        # the remote is untouched (read-only)
        assert (remote / "data-0.btrfs.age").exists()
        assert sorted(p.name for p in remote.iterdir()) == [
            "data-0.btrfs.age", "junk.txt", "manifest-0.json",
            "manifest-0.json.sig", "manifest-2.json"]


class TestArgparse:
    def test_rehearse_subcommand_defaults_read_only(self):
        p = svc._build_argparser()
        args = p.parse_args(["rehearse", "--config", "/c"])
        assert args.fn is svc.cmd_rehearse
        assert args.rehearse_receive is False      # read-only by default
        assert args.identity_file is None

    def test_rehearse_receive_is_opt_in(self):
        p = svc._build_argparser()
        args = p.parse_args(["rehearse", "--rehearse-receive",
                             "--identity-file", "/id.txt",
                             "--rehearse-subvol", "data"])
        assert args.rehearse_receive is True
        assert args.identity_file == "/id.txt"
        assert args.rehearse_subvol == "data"

    def test_run_subcommand_still_present(self):
        p = svc._build_argparser()
        args = p.parse_args(["run", "--config", "/c"])
        assert args.fn is svc.cmd_run


class TestReceiveGuards:
    def test_receive_without_identity_file_fails(self, tmp_path, capsys):
        # _rehearse_receive needs the rage identity; missing -> loud failure
        from types import SimpleNamespace
        cli = sys.modules["qdistro_backup_cli"]
        mf = sys.modules["qdistro_backup_manifest"]
        cfg = {"state_dir": str(tmp_path / "state")}
        args = SimpleNamespace(identity_file=None, rehearse_subvol=None,
                               receive_cmd="btrfs receive",
                               snapshot_delete_cmd="btrfs subvolume delete")
        rc = svc._rehearse_receive(args, cfg, cli, mf, str(tmp_path),
                                   [], ["data"])
        assert rc == 1
        assert "identity-file" in capsys.readouterr().err
