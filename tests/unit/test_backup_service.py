"""Unit coverage for the daily backup-service DRIVER (qdistro_backup_service).

The full orchestration (config -> snapshot -> engine -> push -> verify ->
advance) is exercised end-to-end with real rage + signed manifests by
tests/integration/backup-e2e.bats (btrfs tar-stubbed, local-dir remote). These
pure-Python tests pin the pieces a bats lane reads less precisely: TOML config
validation (fail-closed), the local seq/manifest state machine (atomic +
host-bound), the snapshot-pruning + incremental-parent decisions, the
target-kind selection, and the ssh transport argv shape (no execution).
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path

import pytest

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


def _write_conf(tmp_path, body: str) -> str:
    p = tmp_path / "backup.conf"
    p.write_text(body)
    return str(p)


_MINIMAL = """
host_id = "h1"
recipients = "/etc/qdistro/backup-recipients.txt"
remote = "/srv/backup/h1"
sign_key = "/etc/qdistro/backup-sign-ed25519"
[[subvol]]
name = "data"
source = "/home/silos/alice"
"""


class TestConfig:
    def test_minimal_valid_config_normalises(self, tmp_path):
        cfg = svc.load_config(_write_conf(tmp_path, _MINIMAL))
        assert cfg["host_id"] == "h1"
        assert cfg["remote"] == "/srv/backup/h1"
        # defaults derive from state_dir
        assert cfg["state_dir"] == svc.DEFAULT_STATE_DIR
        assert cfg["snapshot_dir"].endswith("/snapshots")
        assert cfg["scratch_dir"].endswith("/staging")
        assert len(cfg["subvols"]) == 1
        assert cfg["subvols"][0] == {
            "name": "data", "collector": False, "source": "/home/silos/alice"}

    @pytest.mark.parametrize("drop",
                             ["host_id", "recipients", "remote", "sign_key"])
    def test_missing_required_top_level_field_fails(self, tmp_path, drop):
        body = "\n".join(line for line in _MINIMAL.strip().splitlines()
                         if not line.startswith(drop + " "))
        with pytest.raises(svc.BackupServiceError):
            svc.load_config(_write_conf(tmp_path, body))

    def test_no_subvol_fails(self, tmp_path):
        body = 'host_id="h"\nrecipients="r"\nremote="/x"\n'
        with pytest.raises(svc.BackupServiceError):
            svc.load_config(_write_conf(tmp_path, body))

    def test_bad_subvol_name_rejected(self, tmp_path):
        body = _MINIMAL.replace('name = "data"', 'name = "../evil"')
        with pytest.raises(svc.BackupServiceError):
            svc.load_config(_write_conf(tmp_path, body))

    def test_duplicate_subvol_name_rejected(self, tmp_path):
        body = _MINIMAL + '\n[[subvol]]\nname = "data"\nsource = "/other"\n'
        with pytest.raises(svc.BackupServiceError):
            svc.load_config(_write_conf(tmp_path, body))

    def test_plain_subvol_without_source_fails(self, tmp_path):
        body = 'host_id="h"\nrecipients="r"\nremote="/x"\n[[subvol]]\nname="d"\n'
        with pytest.raises(svc.BackupServiceError):
            svc.load_config(_write_conf(tmp_path, body))

    def test_collector_needs_paths(self, tmp_path):
        body = (_MINIMAL
                + '\n[[subvol]]\nname = "meta"\ncollector = true\n')
        with pytest.raises(svc.BackupServiceError):
            svc.load_config(_write_conf(tmp_path, body))

    def test_collector_with_paths_ok(self, tmp_path):
        body = (_MINIMAL + '\n[[subvol]]\nname = "meta"\ncollector = true\n'
                'paths = ["/etc/qdistro"]\nexclude = ["*.sock"]\n')
        cfg = svc.load_config(_write_conf(tmp_path, body))
        meta = [s for s in cfg["subvols"] if s["name"] == "meta"][0]
        assert meta["collector"] and meta["paths"] == ["/etc/qdistro"]
        assert meta["exclude"] == ["*.sock"]

    def test_malformed_toml_fails(self, tmp_path):
        with pytest.raises(svc.BackupServiceError):
            svc.load_config(_write_conf(tmp_path, "this is not = toml ]["))


class TestTargetSelection:
    def test_local_dir_target_for_bare_path(self):
        t = svc.make_target("/srv/backup/h1", "rsync -aHAX", "ssh")
        assert isinstance(t, svc.LocalDirTarget)

    def test_local_dir_target_for_relative_path(self):
        t = svc.make_target("backups/h1", "rsync -aHAX", "ssh")
        assert isinstance(t, svc.LocalDirTarget)

    def test_ssh_target_for_user_host_spec(self):
        t = svc.make_target("user@nas:/backups/h1", "rsync -aHAX", "ssh")
        assert isinstance(t, svc.SshTarget)
        assert t.host == "user@nas" and t.base == "/backups/h1"

    def test_ssh_argv_shape(self):
        t = svc.make_target("u@h:/b", "rsync -aHAX", "ssh -F /dev/null")
        # rsync pushes over the SAME configured ssh command (not bare "ssh")
        assert t._rsync_argv("/local/x", "x") == [
            "rsync", "-aHAX", "-e", "ssh -F /dev/null", "/local/x", "u@h:/b/x"]
        assert t._sha256_argv("x") == [
            "ssh", "-F", "/dev/null", "u@h", "sha256sum", "/b/x"]

    def test_ssh_host_leading_dash_refused(self):
        with pytest.raises(svc.BackupServiceError):
            svc.make_target("-oProxyCommand=evil:/b", "rsync", "ssh")


class TestSshTargetExecution:
    """Drive the SshTarget's REAL subprocess paths (ensure/put/commit/sha256)
    against tiny fake `ssh`/`rsync` shims that emulate a remote as a local
    directory. This locks down the actual execution — the mv-based commit
    publish and the `sha256sum`-output parsing — not just the argv shape, so a
    regression in those (which the VM lane proves over real ssh) is also caught
    on the headless host. The fakes accept the EXACT argv the target builds:
        ssh  <host> mkdir -p <dir>      | ssh <host> mv <a> <b> | ssh <host> sha256sum <path>
        rsync ... -e <ssh-words> <src> <host>:<dst>
    treating <host>: as a no-op prefix so paths land in a local 'remote' root.
    """

    def _shims(self, tmp_path):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        # Fake ssh: strips the leading ssh options + host token, runs the rest
        # as a plain local command (mkdir/mv/sha256sum all operate on real local
        # paths because the probe's remote path == a local path under tmp).
        ssh = bindir / "ssh"
        oplog = bindir / "ops.log"
        ssh.write_text(
            "#!/bin/bash\n"
            "# skip ssh options until we hit the host token (no leading '-')\n"
            "while [[ \"$1\" == -* ]]; do\n"
            "  # options like -i/-p/-o take an argument; -F too. consume both.\n"
            "  case \"$1\" in -i|-p|-o|-F) shift 2 ;; *) shift ;; esac\n"
            "done\n"
            "shift   # drop the host token\n"
            f"echo \"$1\" >> {oplog}   # record the remote op (mkdir/mv/sync/...)\n"
            "exec \"$@\"\n")
        ssh.chmod(0o755)
        self._oplog = oplog
        # Fake rsync: last two args are <src> and <host>:<dst>; copy locally.
        rsync = bindir / "rsync"
        rsync.write_text(
            "#!/bin/bash\n"
            "args=(\"$@\")\n"
            "src=\"${args[-2]}\"\n"
            "dst=\"${args[-1]}\"\n"
            "dst=\"${dst#*:}\"   # strip 'host:' prefix\n"
            "cp -- \"$src\" \"$dst\"\n")
        rsync.chmod(0o755)
        return str(ssh), str(rsync)

    def test_ssh_target_round_trips_over_fake_ssh_rsync(self, tmp_path):
        ssh, rsync = self._shims(tmp_path)
        remote_root = tmp_path / "remote"
        # bare local path as the "remote dir"; host token is a placeholder.
        spec = f"fakehost:{remote_root}/dest"
        t = svc.make_target(spec, f"{rsync} -aHAX", ssh)
        assert isinstance(t, svc.SshTarget)

        t.ensure()                                   # ssh ... mkdir -p <dir>
        assert (remote_root / "dest").is_dir()

        blob = tmp_path / "blob.bin"
        blob.write_bytes(b"hello-ssh-transport")
        t.put(str(blob), "blob.bin")                 # rsync push
        landed = remote_root / "dest" / "blob.bin"
        assert landed.read_bytes() == b"hello-ssh-transport"

        # readback (sha256sum over ssh) must equal the real local hash and parse
        # the FIRST whitespace field of `sha256sum`'s "<hash>  <path>" output.
        mf = svc._load_mod("qdistro_backup_manifest")
        assert t.sha256("blob.bin") == mf.sha256_file(str(blob))
        assert t.sha256("does-not-exist") is None    # remote sha256sum fails -> None

        # commit() must upload to <name>.upload.tmp then `ssh mv` to <name>,
        # leaving NO .upload.tmp behind (the atomic publish of the commit marker).
        manifest = tmp_path / "manifest-0.json"
        manifest.write_bytes(b'{"seq":0}')
        t.commit(str(manifest), "manifest-0.json")
        published = remote_root / "dest" / "manifest-0.json"
        assert published.read_bytes() == b'{"seq":0}'
        assert not (remote_root / "dest" / "manifest-0.json.upload.tmp").exists()

        # commit() must end with a REMOTE durability barrier (`ssh <host> sync`)
        # so the published bytes are crash-durable on the target before the
        # driver reads them back and advances local state (codex review).
        ops = self._oplog.read_text().split()
        assert "mv" in ops and "sync" in ops
        assert ops.index("sync") > ops.index("mv"), \
            "the remote sync must run AFTER the publish mv (flushes the rename)"

    def _read_shim(self, tmp_path):
        """A fake ssh tailored to the rehearsal's READ path (listdir/get), which
        now passes the remote command as ONE shell-quoted token. Real ssh hands
        that single token to the remote LOGIN SHELL, so this shim re-parses it as
        a shell does — `bash -c "$1"` — not `exec "$@"` (which would treat the
        `find ... -printf %f\\0` token as a program name). NOTE: this models the
        single-token form only; it is not a general OpenSSH model (an op passing
        multiple separate tokens would need `bash -c "$*"`). That single-token
        re-parse is exactly where the VM lane caught the bug: the old argv form
        lost the `-printf` separator through the remote shell, so the old code
        run under THIS shim returns mangled/empty names (a true regression
        guard, not a tautology)."""
        bindir = tmp_path / "rbin"
        bindir.mkdir()
        ssh = bindir / "ssh"
        ssh.write_text(
            "#!/bin/bash\n"
            "while [[ \"$1\" == -* ]]; do\n"
            "  case \"$1\" in -i|-p|-o|-F) shift 2 ;; *) shift ;; esac\n"
            "done\n"
            "shift   # drop the host token\n"
            "exec bash -c \"$1\"   # remote login shell re-parses the joined command\n")
        ssh.chmod(0o755)
        rsync = bindir / "rsync"
        rsync.write_text(
            "#!/bin/bash\n"
            "args=(\"$@\")\n"
            "src=\"${args[-2]}\"\n"
            "dst=\"${args[-1]}\"\n"
            "src=\"${src#*:}\"   # strip optional 'host:' on the PULL source\n"
            "dst=\"${dst#*:}\"   # strip optional 'host:' on the PUSH dest\n"
            "cp -- \"$src\" \"$dst\"\n")
        rsync.chmod(0o755)
        return str(ssh), str(rsync)

    def test_ssh_listdir_get_read_only_paths(self, tmp_path):
        # The rehearsal's read-only remote access: SshTarget.listdir (manifest
        # discovery) + SshTarget.get (manifest/blob pull). listdir must survive
        # the remote-shell re-parse and return CLEAN separated basenames — the
        # regression the VM lane caught (the old `-printf %f\\n` argv form
        # collapsed every name together once it crossed ssh).
        ssh, rsync = self._read_shim(tmp_path)
        remote_root = tmp_path / "remote2"
        (remote_root / "dest").mkdir(parents=True)
        for n in ("manifest-0.json", "manifest-0.json.sig", "data-0.btrfs.age"):
            (remote_root / "dest" / n).write_text(n)
        # a subdir at the base must NOT appear (find -type f only)
        (remote_root / "dest" / "subdir").mkdir()
        spec = f"fakehost:{remote_root}/dest"
        t = svc.make_target(spec, f"{rsync} -aHAX", ssh)
        assert isinstance(t, svc.SshTarget)

        names = set(t.listdir())
        assert names == {"manifest-0.json", "manifest-0.json.sig", "data-0.btrfs.age"}, \
            f"listdir over (faked) ssh returned mangled/wrong names: {names!r}"

        # get() pulls a remote artifact down to a local path (read-only).
        local = tmp_path / "pulled.json"
        t.get("manifest-0.json", str(local))
        assert local.read_text() == "manifest-0.json"

        # An ABSENT remote base yields [] (find exits nonzero) — never raises.
        t_missing = svc.make_target(
            f"fakehost:{remote_root}/nope", f"{rsync} -aHAX", ssh)
        assert t_missing.listdir() == []


class TestState:
    def _state(self, tmp_path):
        return svc.State(str(tmp_path / "state"), "h1")

    def test_fresh_state_defaults(self, tmp_path):
        st = self._state(tmp_path)
        st.load()
        assert st.last_seq == -1
        assert st.prev_manifest_path() is None
        assert st.subvol("data") == {}

    def test_commit_is_atomic_and_round_trips(self, tmp_path):
        st = self._state(tmp_path)
        st.load()
        st.commit(0, {"data": {"parent_snapshot": "/s/0/data"}})
        # no stray temp files left behind
        assert not any(n.startswith(".state-")
                       for n in os.listdir(str(tmp_path / "state")))
        st2 = self._state(tmp_path)
        st2.load()
        assert st2.last_seq == 0
        assert st2.subvol("data")["parent_snapshot"] == "/s/0/data"

    def test_prev_manifest_path_requires_the_file(self, tmp_path):
        st = self._state(tmp_path)
        st.load()
        st.commit(3, {})
        assert st.prev_manifest_path() is None      # manifest file absent
        os.makedirs(st.manifests_dir, exist_ok=True)
        (Path(st.manifests_dir) / "manifest-3.json").write_text("{}")
        assert st.prev_manifest_path().endswith("manifest-3.json")

    def test_corrupt_state_raises(self, tmp_path):
        st = self._state(tmp_path)
        os.makedirs(st.state_dir, exist_ok=True)
        Path(st.path).write_text("{ not json")
        with pytest.raises(svc.BackupServiceError):
            st.load()


class TestSnapshotDecisions:
    def test_prune_sweeps_only_dirs_below_seq(self, tmp_path):
        snapdir = tmp_path / "snapshots"
        for s in (0, 1, 2):
            (snapdir / str(s) / "data").mkdir(parents=True)
        (snapdir / "keepme").mkdir()        # non-numeric -> never touched
        svc._prune_snapshot_dirs_below(str(snapdir), 2, "rm -rf")
        assert not (snapdir / "0").exists()
        assert not (snapdir / "1").exists()
        assert (snapdir / "2" / "data").exists()   # current seq kept as parent
        assert (snapdir / "keepme").exists()

    def test_prev_has_subvol(self, tmp_path):
        mf = sys.modules["qdistro_backup_manifest"]
        m = mf.build_manifest(
            seq=0, host_id="h", created_at=1,
            entries=[mf.build_entry("data", "data-0.btrfs.age", "a" * 64, 1, None)],
            prev_manifest_sha256=None)
        p = tmp_path / "manifest-0.json"
        p.write_bytes(mf.manifest_canonical_bytes(m))
        assert svc._prev_has_subvol(mf, str(p), "data") is True
        assert svc._prev_has_subvol(mf, str(p), "meta") is False
        assert svc._prev_has_subvol(mf, str(tmp_path / "nope.json"), "data") is False


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
class TestCollector:
    # The collector dest must be a real subvolume in production; the host stub
    # uses `mkdir -p` for --subvol-create-cmd.
    CREATE = "mkdir -p"

    def test_collects_present_paths_skips_absent(self, tmp_path):
        src_a = tmp_path / "etcq"
        (src_a).mkdir()
        (src_a / "silos.yaml").write_text("alice\n")
        sv = {"name": "metadata", "collector": True,
              "paths": [str(src_a), str(tmp_path / "absent")], "exclude": []}
        dest = str(tmp_path / "stage")
        out = svc.collect_metadata(sv, dest, "rsync -a", self.CREATE)
        assert out == dest
        # the present path landed under a per-source subdir; absent one skipped
        sub = os.path.join(dest, svc._src_subdir(str(src_a)))
        assert os.path.isfile(os.path.join(sub, "silos.yaml"))

    def test_src_subdir_disambiguates_colliding_mangles(self):
        # /a/b and /a_b both mangle to "a_b" — the hash suffix keeps them apart
        assert svc._src_subdir("/a/b") != svc._src_subdir("/a_b")

    def test_rebuilds_fresh_each_run(self, tmp_path):
        src = tmp_path / "etcq"
        src.mkdir()
        (src / "a").write_text("1")
        sv = {"name": "m", "collector": True, "paths": [str(src)], "exclude": []}
        dest = str(tmp_path / "stage")
        svc.collect_metadata(sv, dest, "rsync -a", self.CREATE)
        sub = os.path.join(dest, svc._src_subdir(str(src)))
        assert os.path.isfile(os.path.join(sub, "a"))
        # remove the source file; a re-collect must drop it from the stage too
        (src / "a").unlink()
        (src / "b").write_text("2")
        svc.collect_metadata(sv, dest, "rsync -a", self.CREATE)
        assert not os.path.exists(os.path.join(sub, "a"))
        assert os.path.isfile(os.path.join(sub, "b"))

    def test_dropped_source_cleared_on_recollect(self, tmp_path):
        src1, src2 = tmp_path / "one", tmp_path / "two"
        src1.mkdir(); (src1 / "x").write_text("1")
        src2.mkdir(); (src2 / "y").write_text("2")
        dest = str(tmp_path / "stage")
        sv = {"name": "m", "collector": True,
              "paths": [str(src1), str(src2)], "exclude": []}
        svc.collect_metadata(sv, dest, "rsync -a", self.CREATE)
        assert os.path.isdir(os.path.join(dest, svc._src_subdir(str(src2))))
        # drop src2 from config; its staged subdir must disappear
        sv["paths"] = [str(src1)]
        svc.collect_metadata(sv, dest, "rsync -a", self.CREATE)
        assert not os.path.exists(os.path.join(dest, svc._src_subdir(str(src2))))
        assert os.path.isdir(os.path.join(dest, svc._src_subdir(str(src1))))
