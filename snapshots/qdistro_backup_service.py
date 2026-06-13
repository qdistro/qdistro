"""qdistro-backup-run — the daily backup-service DRIVER.

Wires the live ``qdistro-backup.service`` to the signed-manifest engine
(``qdistro_backup_cli`` / ``qdistro_backup_manifest``) over a SUBVOLUME SET
read from ``/etc/qdistro/backup.conf`` (TOML), replacing the old single-subvol
unsigned ``qdistro-snap-export`` path (06-backup-dr §2-§4).

What the driver owns that the engine does not:

- **Config**: a TOML subvol set + recipients + remote target + signing key.
- **Monotonic seq + chain anchor**: a LOCAL state file + manifest store under
  ``state_dir`` (default /var/lib/qdistro/backup). The SOURCE host is the chain
  authority at backup time — the driver never lets the (possibly hostile)
  target supply the previous manifest. The off-machine owner checkpoint stays
  the DR anti-rollback anchor; local state is operational continuity only.
- **Driver-owned RO snapshots**: ``btrfs subvolume snapshot -r`` per source,
  the previous run's snapshot as the incremental ``-p`` parent. A missing /
  unverifiable parent falls back to a full send rather than an unanchored
  incremental.
- **Minimal metadata collector**: a ``collector`` subvol stages a configured
  config-file set into a directory that is then snapshot+sent like any other
  subvol (no tar side-channel — everything is a manifest entry).
- **Stage-then-push transport**: the engine writes a local staging dir; the
  driver mirrors it to the remote (blobs first, the manifest LAST as the commit
  marker), reads the remote back, and only then advances state and prunes.

The critical ordering (codex review — never advance state for a run the target
does not durably hold):

  lock -> read state -> collect metadata -> snapshot subvols -> engine into
  staging -> push to remote -> verify remote readback -> store manifest locally
  -> atomically advance state -> prune old snapshots + delete local blobs.

Every external command is injectable so the host e2e lane runs with btrfs
tar-stubbed and a local-directory "remote" (rage + ssh-keygen signing are real);
real ``btrfs send/receive`` + real ssh/rsync transport are the VM residual.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from types import SimpleNamespace

# --------------------------------------------------------------------------
# engine imports (installed path first, then in-tree — mirrors the CLIs)
# --------------------------------------------------------------------------

def _load_mod(name: str):
    try:
        return __import__(name)
    except ImportError:
        pass
    import importlib.util
    path = os.path.join(os.path.dirname(__file__), f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class BackupServiceError(Exception):
    pass


def _mkdir_private(path: str) -> None:
    """Create ``path`` 0700. The snapshot + collect dirs hold UNENCRYPTED
    silo/metadata bytes on local disk (btrfs snapshots are plaintext; the
    collector stages /etc/qdistro copies), so they must never be world- or
    group-readable. chmod after makedirs in case the dir pre-existed looser."""
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

DEFAULT_CONFIG = "/etc/qdistro/backup.conf"
DEFAULT_STATE_DIR = "/var/lib/qdistro/backup"


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise BackupServiceError(f"backup.conf: {msg}")


def _safe_name(value: str) -> str:
    """A subvol name becomes a blob/path component — reject anything that is
    not a single benign component (mirrors the manifest layer's check, applied
    here so a bad config fails before any snapshot/send)."""
    _require(isinstance(value, str) and bool(value), "subvol name must be a non-empty string")
    _require("/" not in value and "\\" not in value and value not in (".", "..")
             and not value.startswith(".") and not value.startswith("-")
             and ":" not in value,
             f"subvol name {value!r} must be a single path component "
             "(no '/', '\\', '..', leading '.'/'-', or ':')")
    return value


def load_config(path: str) -> dict:
    """Parse + validate backup.conf (TOML). Returns a normalised config dict.
    Fail closed: a malformed config must abort the run, never silently skip a
    subvol or drop the remote."""
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise BackupServiceError(f"cannot read backup.conf {path!r}: {e}") from e

    _require(isinstance(raw, dict), "top level must be a table")
    host_id = raw.get("host_id")
    _require(isinstance(host_id, str) and bool(host_id), "host_id is required")
    recipients = raw.get("recipients")
    _require(isinstance(recipients, str) and bool(recipients),
             "recipients (age recipients file) is required")
    remote = raw.get("remote")
    _require(isinstance(remote, str) and bool(remote),
             "remote (target dir or user@host:/path) is required")

    # sign_key is REQUIRED for the daily service: an unsigned manifest is one
    # the fail-closed verify/restore path refuses (without --insecure-no-verify),
    # so an unsigned daily backup is silently un-restorable. The engine CLI still
    # allows unsigned for ad-hoc/testing use; the SERVICE must always sign.
    sign_key = raw.get("sign_key")
    _require(isinstance(sign_key, str) and bool(sign_key),
             "sign_key is required (the daily service must produce SIGNED, "
             "restorable manifests)")
    allowed_signers = raw.get("allowed_signers")
    _require(allowed_signers is None or isinstance(allowed_signers, str),
             "allowed_signers must be a string")
    sign_identity = raw.get("sign_identity")
    _require(sign_identity is None or isinstance(sign_identity, str),
             "sign_identity must be a string")

    state_dir = raw.get("state_dir") or DEFAULT_STATE_DIR
    _require(isinstance(state_dir, str), "state_dir must be a string")
    snapshot_dir = raw.get("snapshot_dir") or os.path.join(state_dir, "snapshots")
    scratch_dir = raw.get("scratch_dir") or os.path.join(state_dir, "staging")
    collect_dir = raw.get("collect_dir") or os.path.join(state_dir, "collect")
    # Snapshot/collect paths become part of the engine's ':'-delimited
    # NAME:SNAP[:PARENT] subvol spec — a ':' in them would mis-parse a full send
    # into a bogus incremental. Reject it at load (these are root-set anyway).
    for _label, _p in (("state_dir", state_dir), ("snapshot_dir", snapshot_dir),
                       ("collect_dir", collect_dir)):
        _require(":" not in _p, f"{_label} must not contain ':'")

    subvols_raw = raw.get("subvol")
    _require(isinstance(subvols_raw, list) and bool(subvols_raw),
             "at least one [[subvol]] is required")
    seen: set[str] = set()
    subvols: list[dict] = []
    for s in subvols_raw:
        _require(isinstance(s, dict), "each [[subvol]] must be a table")
        name = _safe_name(s.get("name", ""))
        _require(name not in seen, f"duplicate subvol name {name!r}")
        seen.add(name)
        collector = bool(s.get("collector", False))
        if collector:
            paths = s.get("paths")
            _require(isinstance(paths, list) and bool(paths) and
                     all(isinstance(p, str) and p for p in paths),
                     f"collector subvol {name!r} needs a non-empty 'paths' list")
            excludes = s.get("exclude", [])
            _require(isinstance(excludes, list) and
                     all(isinstance(p, str) for p in excludes),
                     f"subvol {name!r} 'exclude' must be a list of strings")
            subvols.append({"name": name, "collector": True,
                            "paths": list(paths), "exclude": list(excludes)})
        else:
            source = s.get("source")
            _require(isinstance(source, str) and bool(source),
                     f"subvol {name!r} needs a 'source' subvolume path")
            subvols.append({"name": name, "collector": False, "source": source})

    return {
        "host_id": host_id,
        "recipients": recipients,
        "remote": remote,
        "sign_key": sign_key,
        "allowed_signers": allowed_signers,
        "sign_identity": sign_identity,
        "state_dir": state_dir,
        "snapshot_dir": snapshot_dir,
        "scratch_dir": scratch_dir,
        "collect_dir": collect_dir,
        "subvols": subvols,
    }


# --------------------------------------------------------------------------
# local state (the chain anchor) — atomic, locked
# --------------------------------------------------------------------------

class State:
    """Operational continuity record under ``state_dir``:
        {"seq": <last successful seq | -1>,
         "host_id": ...,
         "subvols": {name: {"parent_snapshot": path|null,
                             "parent_blob": blobname|null}}}
    Read/modified in memory; written atomically (temp + fsync + rename + dir
    fsync) only after a run is durable on the remote."""

    def __init__(self, state_dir: str, host_id: str):
        self.state_dir = state_dir
        self.path = os.path.join(state_dir, "state.json")
        self.manifests_dir = os.path.join(state_dir, "manifests")
        self.data = {"seq": -1, "host_id": host_id, "subvols": {}}

    def load(self) -> None:
        try:
            with open(self.path, "rb") as f:
                loaded = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            raise BackupServiceError(f"corrupt state {self.path!r}: {e}") from e
        if not isinstance(loaded, dict) or "seq" not in loaded:
            raise BackupServiceError(f"corrupt state {self.path!r}")
        self.data = loaded
        self.data.setdefault("subvols", {})

    @property
    def last_seq(self) -> int:
        return int(self.data.get("seq", -1))

    def prev_manifest_path(self) -> str | None:
        if self.last_seq < 0:
            return None
        p = os.path.join(self.manifests_dir, f"manifest-{self.last_seq}.json")
        return p if os.path.isfile(p) else None

    def subvol(self, name: str) -> dict:
        return self.data.get("subvols", {}).get(name, {})

    def commit(self, seq: int, subvols: dict) -> None:
        """Atomically advance to ``seq``. ``subvols`` maps name ->
        {"parent_snapshot", "parent_blob"} for the NEXT run's incremental."""
        self.data["seq"] = int(seq)
        self.data["subvols"] = subvols
        _mkdir_private(self.state_dir)
        fd, tmp = tempfile.mkstemp(dir=self.state_dir, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f, sort_keys=True, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        # fsync the directory so the rename is durable.
        try:
            dfd = os.open(self.state_dir, os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass


def _flock(lock_path: str):
    """Exclusive non-blocking lock so an overlapping timer fire cannot reuse a
    seq / race the snapshot set. Returns the open fd (held until process exit);
    raises if another run holds it."""
    _mkdir_private(os.path.dirname(lock_path))
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        raise BackupServiceError(
            f"another qdistro-backup-run holds {lock_path!r}: {e}") from e
    return fd


# --------------------------------------------------------------------------
# remote target — stage-then-push
# --------------------------------------------------------------------------

class LocalDirTarget:
    """A directory on this host or a mounted share. Fully exercised by the host
    lane: push = copy, commit = tmp+rename, readback = direct hash."""

    def __init__(self, path: str):
        self.path = path

    def ensure(self) -> None:
        os.makedirs(self.path, exist_ok=True)

    def put(self, local_path: str, name: str) -> None:
        dst = os.path.join(self.path, name)
        tmp = dst + ".upload.tmp"
        try:
            shutil.copyfile(local_path, tmp)
            os.replace(tmp, dst)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def commit(self, local_path: str, name: str) -> None:
        # Manifest commit marker: identical to put() — os.replace IS the atomic
        # publish, so a reader keyed on the manifest never sees a partial one.
        self.put(local_path, name)

    def sha256(self, name: str) -> str | None:
        mf = _load_mod("qdistro_backup_manifest")
        p = os.path.join(self.path, name)
        if not os.path.isfile(p):
            return None
        return mf.sha256_file(p)


class SshTarget:
    """user@host:/path — rsync -e <ssh> push + ssh remote ops. The argv shape is
    covered by a host unit test, the real execution paths (ensure/put/commit/
    sha256) by a host unit test against fake ssh/rsync shims, and the fully-real
    rsync-over-ssh transport end-to-end by the VM lane
    tests/integration/vm/backup-ssh-e2e.bats (a throwaway localhost sshd)."""

    def __init__(self, spec: str, rsync_cmd: str, ssh_cmd: str):
        self.host, self.base = spec.split(":", 1)
        # A host starting with '-' would be read by ssh/rsync as an option.
        # The remote is root-set, so this is hardening, but cheap and absolute.
        if self.host.startswith("-") or not self.host:
            raise BackupServiceError(
                f"invalid remote host {self.host!r} in {spec!r}")
        self.rsync_base = shlex.split(rsync_cmd)
        self.ssh_base = shlex.split(ssh_cmd)

    def _remote(self, name: str) -> str:
        return f"{self.host}:{self.base.rstrip('/')}/{name}"

    def _rsync_argv(self, local_path: str, name: str) -> list[str]:
        # Push over the SAME ssh command used for the remote ops (readback,
        # mkdir, mv) so a configured key/config/port isn't silently dropped on
        # upload. rsync's -e takes one shell-word string it splits itself.
        return self.rsync_base + ["-e", " ".join(self.ssh_base),
                                  local_path, self._remote(name)]

    def _sha256_argv(self, name: str) -> list[str]:
        return self.ssh_base + [self.host, "sha256sum",
                                f"{self.base.rstrip('/')}/{name}"]

    def ensure(self) -> None:
        subprocess.run(self.ssh_base + [self.host, "mkdir", "-p", self.base],
                       check=True)

    def put(self, local_path: str, name: str) -> None:
        subprocess.run(self._rsync_argv(local_path, name), check=True)

    def commit(self, local_path: str, name: str) -> None:
        # Upload to a temp remote name then rename — the rename is the atomic
        # publish of the commit marker.
        self.put(local_path, name + ".upload.tmp")
        subprocess.run(self.ssh_base + [self.host, "mv",
                       f"{self.base.rstrip('/')}/{name}.upload.tmp",
                       f"{self.base.rstrip('/')}/{name}"], check=True)
        # Remote durability barrier. The manifest is the LAST artifact written
        # (commit marker), so a remote `sync` here flushes the blobs, signature,
        # manifest, AND the directory rename to the target's stable storage
        # before the driver's readback returns and it advances + prunes local
        # state. Without this, a target crash AFTER the bytes are readable-over-
        # ssh but BEFORE they hit disk would lose the just-advanced seq while
        # local state moved on (codex review). `sync` is POSIX and global, so it
        # covers every preceding write in one round trip; fail-closed (check).
        subprocess.run(self.ssh_base + [self.host, "sync"], check=True)

    def sha256(self, name: str) -> str | None:
        proc = subprocess.run(self._sha256_argv(name),
                              stdout=subprocess.PIPE, check=False)
        if proc.returncode != 0:
            return None
        out = proc.stdout.decode("utf-8", "replace").split()
        return out[0] if out else None


def make_target(remote: str, rsync_cmd: str, ssh_cmd: str):
    # A bare local path (no "host:" prefix) is a LocalDirTarget; anything with a
    # ':' before the first '/' is ssh. (An absolute path starts with '/', so it
    # never trips the ssh branch.)
    head = remote.split("/", 1)[0]
    if ":" in head:
        return SshTarget(remote, rsync_cmd, ssh_cmd)
    return LocalDirTarget(remote)


# --------------------------------------------------------------------------
# metadata collector (minimal v1)
# --------------------------------------------------------------------------

def _src_subdir(src: str) -> str:
    """Unambiguous per-source subdir name. A readable mangling of the path PLUS
    a short hash of the ORIGINAL path, so e.g. ``/a/b`` and ``/a_b`` (which both
    mangle to ``a_b``) never collide into one staging dir and silently merge."""
    import hashlib
    mangled = src.strip("/").replace("/", "_") or "root"
    h = hashlib.sha1(src.encode("utf-8")).hexdigest()[:8]
    return f"{mangled}-{h}"


def collect_metadata(sv: dict, dest: str, rsync_cmd: str,
                     subvol_create_cmd: str) -> str:
    """Stage a config-file set into ``dest`` and return ``dest``. ``dest`` must
    be a real (btrfs) SUBVOLUME, because the driver then takes a RO
    ``btrfs subvolume snapshot`` of it like any other subvol — a plain directory
    would make that snapshot fail. So: create ``dest`` as a subvolume on first
    run (idempotent via ``subvol_create_cmd``; injectable so the host lane stubs
    it), CLEAR its children each run (fresh content, incl. dropping a source
    removed from config), then rsync each present source into a per-source
    subdir. The subsequent RO snapshot is the consistent point-in-time copy.

    v1 is deliberately config-only (no live sqlite/audit DBs — raw-copying a hot
    sqlite file is a consistency risk; that coverage is a documented fast-follow).
    Missing source paths are skipped with a warning (a host may not have every
    optional tree); an rsync failure on a PRESENT path aborts the run."""
    parent = os.path.dirname(dest.rstrip("/"))
    _mkdir_private(parent)
    if not os.path.isdir(dest):
        # Create the subvolume once. btrfs refuses to create over an existing
        # path, so only call it when absent; the stub lane uses `mkdir -p`.
        proc = subprocess.run(shlex.split(subvol_create_cmd) + [dest],
                              check=False)
        if proc.returncode != 0:
            raise BackupServiceError(
                f"collector: creating subvolume {dest!r} failed "
                f"(exit {proc.returncode})")
    try:
        os.chmod(dest, 0o700)
    except OSError:
        pass
    # Clear children (regular files/dirs, never the subvol root) for a fresh
    # rebuild — this is what drops a source dropped from config.
    for child in os.listdir(dest):
        p = os.path.join(dest, child)
        if os.path.islink(p) or os.path.isfile(p):
            os.unlink(p)
        elif os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
    rbase = shlex.split(rsync_cmd)
    for src in sv["paths"]:
        if not os.path.exists(src):
            print(f"[collect] WARN: {src!r} absent — skipped", file=sys.stderr)
            continue
        argv = list(rbase) + ["--delete"]
        for ex in sv.get("exclude", []):
            argv += ["--exclude", ex]
        # Trailing slash on src copies its CONTENTS into a per-source subdir;
        # -aHAX (caller's rsync_cmd) preserves perms/acls/xattrs.
        sub = os.path.join(dest, _src_subdir(src))
        os.makedirs(sub, exist_ok=True)
        argv += [src.rstrip("/") + "/", sub + "/"]
        proc = subprocess.run(argv, check=False)
        if proc.returncode != 0:
            raise BackupServiceError(
                f"metadata collect of {src!r} failed (rsync exit "
                f"{proc.returncode})")
    return dest


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------

def _snapshot(snapshot_cmd: str, source: str, dest: str) -> None:
    argv = shlex.split(snapshot_cmd) + [source, dest]
    proc = subprocess.run(argv, check=False)
    if proc.returncode != 0:
        raise BackupServiceError(
            f"snapshot {source!r} -> {dest!r} failed (exit {proc.returncode})")


def _delete_snapshot(delete_cmd: str, path: str) -> None:
    if not path or not os.path.exists(path):
        return
    proc = subprocess.run(shlex.split(delete_cmd) + [path], check=False,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if proc.returncode != 0:
        # Best-effort fallback for the stub lane (a dir, not a real subvol).
        shutil.rmtree(path, ignore_errors=True)


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    mf = _load_mod("qdistro_backup_manifest")
    cli = _load_mod("qdistro_backup_cli")

    # Lock FIRST so an overlapping timer fire cannot race us.
    _flock(os.path.join(cfg["state_dir"], "backup.lock"))

    state = State(cfg["state_dir"], cfg["host_id"])
    state.load()
    if state.data.get("host_id") not in (None, cfg["host_id"]):
        raise BackupServiceError(
            f"state host_id {state.data.get('host_id')!r} != config "
            f"{cfg['host_id']!r} — refusing to mix host chains")
    seq = state.last_seq + 1
    prev_manifest = state.prev_manifest_path()
    # FAIL CLOSED: local state says we are mid-chain (last_seq >= 0) but its
    # manifest is gone from the local store. Proceeding would emit a new
    # manifest with prev_manifest_sha256=null, permanently forking the remote
    # chain (every later run would fail verify) with NO error at backup time.
    # The owner must restore the local manifest store (or, for a deliberate
    # fresh start, reset state.json) — never silently re-anchor.
    if state.last_seq >= 0 and prev_manifest is None:
        raise BackupServiceError(
            f"state.json is at seq {state.last_seq} but its manifest is missing "
            f"from {state.manifests_dir!r} — refusing to break the chain "
            "(restore the local manifest store, or reset state to start fresh)")

    _mkdir_private(cfg["snapshot_dir"])
    _mkdir_private(cfg["collect_dir"])
    staging = tempfile.mkdtemp(prefix="qdistro-backup-", dir=_scratch_parent(cfg))

    # Snapshots taken this run, so we can both feed them to the engine and
    # record them as the NEXT run's parent (and clean up on failure).
    taken: dict[str, str] = {}          # name -> snapshot path (this seq)
    specs: list[str] = []
    next_subvols: dict[str, dict] = {}
    try:
        for sv in cfg["subvols"]:
            name = sv["name"]
            if sv["collector"]:
                source = collect_metadata(
                    sv, os.path.join(cfg["collect_dir"], name), args.rsync_cmd,
                    args.subvol_create_cmd)
            else:
                source = sv["source"]
                if not os.path.exists(source):
                    raise BackupServiceError(
                        f"subvol {name!r}: source {source!r} does not exist")
            # Snapshot layout: <snapshot_dir>/<seq>/<name>. The seq lives in the
            # PARENT dir so the snapshot BASENAME is the stable <name> across
            # every seq. btrfs receive names the received subvol after the sent
            # snapshot's basename; a stable basename is what lets the restore
            # land the final state under --dest/<name> (and lets its per-seq
            # ancestor staging avoid the same-name collision F-A fixed) — a
            # seq-suffixed basename would instead restore to --dest/<name>-<seq>.
            seq_snap_dir = os.path.join(cfg["snapshot_dir"], str(seq))
            os.makedirs(seq_snap_dir, exist_ok=True)
            snap = os.path.join(seq_snap_dir, name)
            _delete_snapshot(args.snapshot_delete_cmd, snap)  # crashed-run leftover
            _snapshot(args.snapshot_cmd, source, snap)
            taken[name] = snap

            # Incremental only if the recorded parent snapshot still exists AND
            # the previous manifest carries this subvol (codex E) — else a full
            # send with parent_blob=null, never an unanchored incremental.
            prev = state.subvol(name)
            parent_snap = prev.get("parent_snapshot")
            if (prev_manifest and parent_snap and os.path.exists(parent_snap)
                    and _prev_has_subvol(mf, prev_manifest, name)):
                specs.append(f"{name}:{snap}:{parent_snap}")
            else:
                specs.append(f"{name}:{snap}")
            next_subvols[name] = {"parent_snapshot": snap}

        # --- engine: encrypt + sign into the local staging dir ---
        ns = SimpleNamespace(
            subvol=specs, recipients=cfg["recipients"], out_dir=staging,
            seq=seq, host_id=cfg["host_id"], created_at=args.now,
            prev_manifest=prev_manifest, sign_key=cfg["sign_key"],
            send_cmd=args.send_cmd)
        rc = cli.cmd_backup(ns)
        if rc != 0:
            raise BackupServiceError(f"engine backup exited {rc}")

        # --- push: blobs first, signature, then the manifest LAST (commit) ---
        target = make_target(cfg["remote"], args.rsync_cmd, args.ssh_cmd)
        target.ensure()
        manifest_name = f"manifest-{seq}.json"
        sig_local = os.path.join(staging, manifest_name + ".sig")
        # sign_key is required, so the engine MUST have produced a signature.
        # Fail closed if it didn't rather than publish an unsigned (un-restorable)
        # manifest the verify/restore path would refuse.
        if not os.path.isfile(sig_local):
            raise BackupServiceError(
                f"engine produced no signature for {manifest_name} "
                "(sign_key configured) — refusing to publish an unsigned backup")
        with open(os.path.join(staging, manifest_name), "rb") as f:
            manifest = mf.parse_manifest(f.read())
        for e in manifest["entries"]:
            target.put(os.path.join(staging, e["blob"]), e["blob"])
        # Signature before the manifest so a reader keyed on the manifest never
        # sees it without its .sig; the manifest is the LAST write (commit marker).
        target.put(sig_local, manifest_name + ".sig")
        target.commit(os.path.join(staging, manifest_name), manifest_name)

        # --- verify the remote actually holds what we signed (readback) ---
        # EVERY artifact a restore needs must read back byte-identical before we
        # advance state: the blobs AND the manifest AND its signature. Checking
        # only blob hashes + manifest EXISTENCE would let a truncated/corrupt/
        # substituted manifest or a corrupt .sig commit advance state, leaving
        # no restorable signed manifest at this seq (the next run moves on to
        # seq+1). The manifest is the restore gate, so verify its bytes too.
        expected = {e["blob"]: e["sha256"] for e in manifest["entries"]}
        expected[manifest_name] = mf.sha256_file(
            os.path.join(staging, manifest_name))
        expected[manifest_name + ".sig"] = mf.sha256_file(sig_local)
        for name, want in expected.items():
            got = target.sha256(name)
            if got != want:
                raise BackupServiceError(
                    f"remote readback mismatch for {name}: {got} != {want} "
                    "— NOT advancing state")

        # --- store the canonical manifest locally (the chain anchor) ---
        # Durable copy (tmp+fsync+rename) BEFORE state.commit: state.json points
        # at this file as the next run's --prev-manifest, so a torn/truncated
        # copy would wedge every subsequent run (M1's guard would then fire).
        _mkdir_private(state.manifests_dir)
        for fn in (manifest_name, manifest_name + ".sig"):
            src = os.path.join(staging, fn)
            if os.path.isfile(src):
                _durable_copy(src, os.path.join(state.manifests_dir, fn))

        # --- advance state ATOMICALLY (only now is the run durable) ---
        state.commit(seq, next_subvols)
    except BaseException:
        # Failed run: drop this seq's snapshots (and the now-empty seq dir) so a
        # retry re-takes them clean, and never leave staged blobs behind. State
        # is NOT advanced — the next timer fire retries the SAME seq.
        seq_dirs = set()
        for snap in taken.values():
            _delete_snapshot(args.snapshot_delete_cmd, snap)
            seq_dirs.add(os.path.dirname(snap))
        for d in seq_dirs:
            try:
                os.rmdir(d)
            except OSError:
                pass
        shutil.rmtree(staging, ignore_errors=True)
        raise

    # --- prune: only AFTER state advanced. We keep ONLY the just-taken (seq)
    # snapshots as the next run's parents, so EVERY <snapshot_dir>/<s> with
    # s < seq is now superseded — a single sweep reclaims this run's superseded
    # parents AND any older dirs orphaned by a crash or a subvol dropped from
    # config (self-healing: a crash before this point is cleaned next run).
    _prune_snapshot_dirs_below(cfg["snapshot_dir"], seq, args.snapshot_delete_cmd)
    shutil.rmtree(staging, ignore_errors=True)
    print(json.dumps({"seq": seq, "subvols": [s["name"] for s in cfg["subvols"]],
                      "remote": cfg["remote"]}))
    return 0


def _scratch_parent(cfg: dict) -> str:
    _mkdir_private(cfg["scratch_dir"])
    return cfg["scratch_dir"]


def _prev_has_subvol(mf, prev_manifest_path: str, name: str) -> bool:
    try:
        with open(prev_manifest_path, "rb") as f:
            prev = mf.parse_manifest(f.read())
    except (OSError, mf.ManifestError):
        return False
    return any(e["subvol"] == name for e in prev["entries"])


def _prune_snapshot_dirs_below(snapshot_dir: str, seq: int,
                               delete_cmd: str) -> None:
    """Delete every <snapshot_dir>/<s> whose integer name is < ``seq`` (each
    holds superseded parent snapshots), subvol children first, then the dir.
    Numeric-named dirs only — never touches anything else under snapshot_dir.
    Best-effort + idempotent: the next run re-sweeps whatever a crash left."""
    try:
        names = os.listdir(snapshot_dir)
    except OSError:
        return
    for name in names:
        if not name.isdigit() or int(name) >= seq:
            continue
        seq_dir = os.path.join(snapshot_dir, name)
        if not os.path.isdir(seq_dir) or os.path.islink(seq_dir):
            continue
        try:
            children = os.listdir(seq_dir)
        except OSError:
            children = []
        for child in children:
            _delete_snapshot(delete_cmd, os.path.join(seq_dir, child))
        try:
            os.rmdir(seq_dir)
        except OSError:
            shutil.rmtree(seq_dir, ignore_errors=True)


def _durable_copy(src: str, dst: str) -> None:
    """Copy ``src`` to ``dst`` crash-durably: write a temp sibling, fsync it,
    atomically rename over ``dst``, then fsync the directory. Used for the local
    manifest store, which state.json then anchors as the next --prev-manifest."""
    d = os.path.dirname(dst) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".mf-", suffix=".tmp")
    try:
        with open(src, "rb") as s, os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(s, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(d, os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


# --------------------------------------------------------------------------
# argparse
# --------------------------------------------------------------------------

def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qdistro-backup-run")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run one backup over the configured subvol set")
    r.add_argument("--config", default=DEFAULT_CONFIG)
    r.add_argument("--now", type=int, default=None,
                   help="created_at override (default: current time)")
    # Injectable command boundary (defaults are production; the host lane stubs
    # btrfs + points the remote at a local dir).
    r.add_argument("--snapshot-cmd", default="btrfs subvolume snapshot -r")
    r.add_argument("--snapshot-delete-cmd", default="btrfs subvolume delete")
    r.add_argument("--subvol-create-cmd", default="btrfs subvolume create",
                   help="create the collector's staging subvolume")
    r.add_argument("--send-cmd", default="btrfs send")
    r.add_argument("--rsync-cmd", default="rsync -aHAX")
    r.add_argument("--ssh-cmd", default="ssh")
    r.set_defaults(fn=cmd_run)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    if args.now is None:
        args.now = int(time.time())
    try:
        return args.fn(args)
    except BackupServiceError as e:
        print(f"qdistro-backup-run: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
