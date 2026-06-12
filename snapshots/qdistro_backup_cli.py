"""qdistro-backup — orchestrate the signed, verifiable backup lifecycle
(06-backup-dr §3-§4): backup -> verify -> restore over the
``send | rage -e | ssh`` pipeline, with a per-run signed manifest.

This wraps the low-level render in qdistro_snapshots.render_backup_command
with the manifest layer (qdistro_backup_manifest) so a restore can prove the
blob set is authentic, gapless and fresh BEFORE trusting it.

Security posture (fail-closed by default):
- verify/restore REQUIRE ``--allowed-signers`` (signature check) unless the
  operator passes ``--insecure-no-verify`` explicitly. A DR tool must not be
  one forgotten flag away from restoring an attacker-authored backup.
- restore copies each blob to a private local file, re-hashes it THERE, and
  receives from that verified copy — closing the verify/receive TOCTOU when
  ``--out-dir`` is attacker-mediated storage (sshfs/NFS onto the target).
- the full manifest chain is loaded, every signature checked, and
  verify_chain (strictly-increasing seq + hash chain + gapless per-subvol
  parent lineage) run before any ``btrfs receive``.

Transport is injectable so the e2e bats lane can run on a host without root
or a btrfs filesystem (btrfs send/receive need CAP_SYS_ADMIN):
  --send-cmd     default "btrfs send"     (``-p PARENT`` inserted for incrs)
  --receive-cmd  default "btrfs receive"  (DEST appended)
The "remote" is a local directory (--out-dir); production uses ssh + btrfs.
rage is always real. No shell is used — argv lists only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

# Manifest engine — installed path first, then in-tree (mirrors the export
# CLI's _load_eng pattern).
def _load_manifest():
    try:
        import qdistro_backup_manifest as m  # type: ignore
        return m
    except ImportError:
        pass
    import importlib.util
    path = os.path.join(os.path.dirname(__file__), "qdistro_backup_manifest.py")
    spec = importlib.util.spec_from_file_location("qdistro_backup_manifest", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["qdistro_backup_manifest"] = mod
    spec.loader.exec_module(mod)
    return mod


MANIFEST_RE = re.compile(r"^manifest-(\d+)\.json$")
RAGE = os.environ.get("QDISTRO_RAGE", "rage")


def _manifest_name(seq: int) -> str:
    return f"manifest-{seq}.json"


class BackupError(Exception):
    pass


def _kill(proc) -> None:
    try:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass


def _encrypt_to_blob(send_argv: list[str], recipients_file: str,
                     blob_path: str) -> None:
    """send_argv | rage -e -R recipients > blob_path. Writes to a temp file
    in the same dir and renames only after BOTH sides exit 0 — a failed send
    never leaves a truncated blob the manifest would then sign as good."""
    tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(blob_path) or ".",
                                        prefix=".bk-", suffix=".tmp")
    p_send = p_rage = None
    try:
        with os.fdopen(tmp_fd, "wb") as out:
            p_send = subprocess.Popen(send_argv, stdout=subprocess.PIPE)
            p_rage = subprocess.Popen(
                [RAGE, "-e", "-R", recipients_file],
                stdin=p_send.stdout, stdout=out)
            if p_send.stdout is not None:
                p_send.stdout.close()
            rage_rc = p_rage.wait()
            send_rc = p_send.wait()
        if send_rc != 0:
            raise BackupError(f"send ({send_argv[0]}) exited {send_rc}")
        if rage_rc != 0:
            raise BackupError(f"rage -e exited {rage_rc}")
        os.replace(tmp_path, blob_path)
    except BaseException:
        for p in (p_send, p_rage):
            if p is not None and p.poll() is None:
                _kill(p)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _decrypt_to_receiver(blob_path: str, identity_file: str,
                         receive_argv: list[str]) -> None:
    """rage -d -i identity < blob | receive_argv. Both exit codes checked."""
    p_rage = p_recv = None
    try:
        with open(blob_path, "rb") as src:
            p_rage = subprocess.Popen(
                [RAGE, "-d", "-i", identity_file],
                stdin=src, stdout=subprocess.PIPE)
            p_recv = subprocess.Popen(receive_argv, stdin=p_rage.stdout)
            if p_rage.stdout is not None:
                p_rage.stdout.close()
            recv_rc = p_recv.wait()
            rage_rc = p_rage.wait()
        if recv_rc != 0:
            raise BackupError(f"receive ({receive_argv[0]}) exited {recv_rc}")
        if rage_rc != 0:
            raise BackupError(f"rage -d exited {rage_rc}")
    except BaseException:
        for p in (p_rage, p_recv):
            if p is not None and p.poll() is None:
                _kill(p)
        raise


def _parse_subvol_spec(mf, spec: str) -> tuple[str, str, str | None]:
    """NAME:SOURCE[:PARENT_SOURCE] -> (name, source, parent_source|None).
    NAME is validated as a single path component (it becomes a blob name)."""
    parts = spec.split(":")
    if len(parts) not in (2, 3):
        raise BackupError(f"bad --subvol spec {spec!r} (want NAME:SOURCE[:PARENT])")
    name = parts[0]
    mf._safe_component(name, "subvol name")
    source = parts[1]
    parent = parts[2] if len(parts) == 3 and parts[2] else None
    return name, source, parent


# --------------------------------------------------------------------------
# backup
# --------------------------------------------------------------------------

def cmd_backup(args) -> int:
    mf = _load_manifest()
    os.makedirs(args.out_dir, exist_ok=True)
    send_base = shlex.split(args.send_cmd)

    prev_sha = None
    parent_blob_map: dict[str, str] = {}
    if args.prev_manifest:
        with open(args.prev_manifest, "rb") as f:
            prev_bytes = f.read()
        prev_sha = mf.sha256_hex(prev_bytes)
        prev = mf.parse_manifest(prev_bytes)
        for e in prev["entries"]:
            parent_blob_map[e["subvol"]] = e["blob"]

    entries = []
    for spec in args.subvol:
        name, source, parent = _parse_subvol_spec(mf, spec)
        blob_name = f"{name}-{args.seq}.btrfs.age"
        blob_path = os.path.join(args.out_dir, blob_name)
        # An incremental send (parent source given) records the parent's blob
        # from the previous run so the chain's lineage is real, not implied.
        # Resolve it BEFORE encrypting so a missing parent fails fast without
        # leaving an orphan blob on the target.
        parent_blob = None
        if parent:
            parent_blob = parent_blob_map.get(name)
            if parent_blob is None:
                raise BackupError(
                    f"subvol {name!r}: incremental send needs --prev-manifest "
                    "containing the parent blob")
        send_argv = list(send_base)
        if parent:
            send_argv += ["-p", parent]
        send_argv.append(source)
        _encrypt_to_blob(send_argv, args.recipients, blob_path)
        entries.append(mf.build_entry(
            subvol=name, blob=blob_name,
            sha256=mf.sha256_file(blob_path),
            size=os.path.getsize(blob_path),
            parent_blob=parent_blob))

    manifest = mf.build_manifest(
        seq=args.seq, host_id=args.host_id,
        created_at=args.created_at if args.created_at is not None
        else int(time.time()),
        entries=entries, prev_manifest_sha256=prev_sha)
    canonical = mf.manifest_canonical_bytes(manifest)
    manifest_path = os.path.join(args.out_dir, _manifest_name(args.seq))
    with open(manifest_path, "wb") as f:
        f.write(canonical)
    if args.sign_key:
        sig = mf.sign_manifest(canonical, args.sign_key)
        with open(manifest_path + ".sig", "w") as f:
            f.write(sig)
    print(json.dumps({"manifest": manifest_path,
                      "blobs": [e["blob"] for e in entries],
                      "seq": args.seq}))
    return 0


# --------------------------------------------------------------------------
# chain loading + verification (shared by verify / restore)
# --------------------------------------------------------------------------

def _require_signing(allowed_signers, identity, insecure):
    if not insecure and not allowed_signers:
        raise BackupError(
            "refusing to proceed without signature verification; pass "
            "--allowed-signers (and --identity), or --insecure-no-verify to "
            "override explicitly")
    if allowed_signers and not identity:
        raise BackupError("--allowed-signers requires --identity")


def _load_chain(mf, out_dir: str, allowed_signers, identity, insecure
                ) -> list[dict]:
    """Discover manifest-<seq>.json in out_dir, parse + signature-verify each,
    sort by seq, and run verify_chain. Returns the chain oldest-first."""
    paths = []
    for entry in os.listdir(out_dir):
        if MANIFEST_RE.match(entry):
            paths.append(os.path.join(out_dir, entry))
    if not paths:
        raise BackupError(f"no manifest-<seq>.json files in {out_dir}")
    manifests = []
    for path in paths:
        with open(path, "rb") as f:
            canonical = f.read()
        manifest = mf.parse_manifest(canonical)
        if allowed_signers:
            sig_path = path + ".sig"
            if not os.path.isfile(sig_path):
                raise BackupError(f"signature missing for {os.path.basename(path)}")
            with open(sig_path) as f:
                sig = f.read()
            if not mf.verify_signature(canonical, sig, allowed_signers,
                                       identity or ""):
                raise BackupError(
                    f"signature verification FAILED for "
                    f"{os.path.basename(path)}")
        manifests.append(manifest)
    manifests.sort(key=lambda m: m["seq"])
    mf.verify_chain(manifests)
    return manifests


def cmd_verify(args) -> int:
    mf = _load_manifest()
    try:
        _require_signing(args.allowed_signers, args.identity,
                         args.insecure_no_verify)
        chain = _load_chain(mf, args.out_dir, args.allowed_signers,
                            args.identity, args.insecure_no_verify)
        newest = chain[-1]
        if args.checkpoint_seq is not None:
            mf.check_freshness(newest, args.checkpoint_seq)
        else:
            print("WARNING: no --checkpoint-seq; rollback to an older signed "
                  "set is NOT detectable", file=sys.stderr)
        problems = []
        for m in chain:
            problems += mf.verify_blobs(m, args.out_dir)
        if problems:
            for p in problems:
                print(f"BLOB PROBLEM: {p}", file=sys.stderr)
            raise BackupError(f"{len(problems)} blob problem(s)")
    except (BackupError, mf.ManifestError) as e:
        print(f"VERIFY FAILED: {e}", file=sys.stderr)
        return 1
    print(f"VERIFY OK: {len(chain)} manifest(s), newest seq {newest['seq']}")
    return 0


# --------------------------------------------------------------------------
# restore
# --------------------------------------------------------------------------

def _verified_receive(mf, entry: dict, out_dir: str, staging: str,
                      identity_file: str, receive_argv: list[str]) -> bool:
    """Receive ONE blob into receive_argv's dest from a PRIVATE verified copy:
    copy the blob locally, re-hash it there (closing the verify/receive TOCTOU
    when out-dir is attacker-mediated storage), then decrypt+receive that copy.
    Returns True on success; prints the failure and returns False on error."""
    src = os.path.join(out_dir, entry["blob"])
    local = os.path.join(staging, entry["blob"])
    try:
        shutil.copyfile(src, local)
        if mf.sha256_file(local) != entry["sha256"]:
            raise BackupError(
                f"{entry['blob']}: sha256 changed since verification "
                "(target tampering?)")
        _decrypt_to_receiver(local, identity_file, receive_argv)
        return True
    except (BackupError, OSError) as e:
        print(f"RESTORE FAILED on {entry['blob']}: {e}", file=sys.stderr)
        return False
    finally:
        try:
            os.unlink(local)
        except OSError:
            pass


def _delete_received(delete_cmd: list[str], path: str) -> None:
    """Remove a restored intermediate subvolume. A real btrfs received subvol
    needs ``btrfs subvolume delete`` (a plain rmtree cannot unlink a subvol
    root); a stubbed/dir restore (no btrfs) falls back to a recursive remove.
    Best-effort — the final restored state already lives under --dest."""
    try:
        subprocess.run(delete_cmd + [path], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    except (OSError, subprocess.CalledProcessError):
        pass
    shutil.rmtree(path, ignore_errors=True)


def _real_subdirs(path: str) -> list[str]:
    """Names directly under ``path`` that are real directories, NOT symlinks.
    Returns [] on any error. Symlink-safe: when --dest is attacker-mediated
    storage, a planted symlink under the staging dir must never be followed
    (else a recursive delete escapes the staging tree)."""
    try:
        with os.scandir(path) as it:
            return [e.name for e in it
                    if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []


def _purge_chain_dir(chain_dir: str, delete_cmd: list[str]) -> None:
    """Drop the per-seq ancestor staging dir and every received subvol under it.
    Received subvols are read-only, so a bare rmtree cannot unlink them — each is
    removed via _delete_received first. Best-effort and idempotent: run it both
    BEFORE staging (to clear a leftover from a crashed prior run — otherwise the
    stale subvol re-triggers the very same-name receive collision this restore
    path avoids) and AFTER, to leave only the final state under --dest. Only
    touches ``chain_dir`` (a name reserved by restore), never the restored
    subvolume itself, and never follows a symlink out of the staging tree."""
    if os.path.islink(chain_dir):
        # Tampering: a symlink where our staging dir should be. Unlink the link
        # itself (not its target) and stop — never descend through it.
        try:
            os.unlink(chain_dir)
        except OSError:
            pass
        return
    if not os.path.isdir(chain_dir):
        return
    for seq_name in _real_subdirs(chain_dir):
        seq_dir = os.path.join(chain_dir, seq_name)
        for child in _real_subdirs(seq_dir):
            # Only real directories (a received subvol IS a directory, never a
            # symlink) are handed to the subvol-aware deleter; symlinks are left
            # for the final rmtree, which unlinks them without following.
            _delete_received(delete_cmd, os.path.join(seq_dir, child))
    shutil.rmtree(chain_dir, ignore_errors=True)


def cmd_restore(args) -> int:
    mf = _load_manifest()
    receive_base = shlex.split(args.receive_cmd)
    delete_base = shlex.split(args.subvol_delete_cmd)
    try:
        _require_signing(args.allowed_signers, args.identity,
                         args.insecure_no_verify)
        chain = _load_chain(mf, args.out_dir, args.allowed_signers,
                            args.identity, args.insecure_no_verify)
        newest = chain[-1]
        if args.checkpoint_seq is not None:
            mf.check_freshness(newest, args.checkpoint_seq)
        else:
            print("WARNING: no --checkpoint-seq; rollback to an older signed "
                  "set is NOT detectable", file=sys.stderr)
        # Full blob verification before receiving ANYTHING.
        problems = []
        for m in chain:
            problems += mf.verify_blobs(m, args.out_dir)
        if problems:
            for p in problems:
                print(f"BLOB PROBLEM: {p}", file=sys.stderr)
            raise BackupError("refusing to restore: blob verification failed")
        order = mf.restore_order(chain, args.subvol)
    except (BackupError, mf.ManifestError) as e:
        print(f"RESTORE ABORTED: {e}", file=sys.stderr)
        return 1

    # ``order`` is oldest->newest: a full send then each incremental. On real
    # btrfs, ``btrfs receive`` names the received subvolume after the SENT
    # snapshot's basename, which is identical across runs (e.g. "data"), so
    # receiving the whole chain into one dest collides on the second seq
    # ("File exists"). Receive each incremental ANCESTOR into its own per-seq
    # staging subdir on the same filesystem as --dest (btrfs still locates each
    # parent by UUID filesystem-wide, so the delta applies); receive the FINAL
    # seq straight into --dest, exposing the restored state under the stable
    # name (a single-seq full restore has no ancestors and is byte-for-byte the
    # old path). The ancestors are then dropped — only the final state is kept.
    *ancestors, final = order
    chain_dir = os.path.join(args.dest, ".qd-restore-chain")
    staging = tempfile.mkdtemp(prefix="qdistro-restore-")
    try:
        if ancestors:
            # Clear any chain dir left by a crashed prior run before reusing it.
            _purge_chain_dir(chain_dir, delete_base)
        for i, entry in enumerate(ancestors):
            seq_dir = os.path.join(chain_dir, str(i))
            try:
                os.makedirs(seq_dir, exist_ok=True)
            except OSError as e:
                print(f"RESTORE FAILED on {entry['blob']}: {e}", file=sys.stderr)
                return 1
            if not _verified_receive(mf, entry, args.out_dir, staging,
                                     args.identity_file,
                                     receive_base + [seq_dir]):
                return 1
        if not _verified_receive(mf, final, args.out_dir, staging,
                                 args.identity_file,
                                 receive_base + [args.dest]):
            return 1
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        # Drop the intermediate received subvolumes; the restored state now
        # lives under --dest and no longer depends on them. Only touched when
        # there were ancestors, so a single-seq full restore leaves --dest
        # byte-for-byte as the pre-fix code did.
        if ancestors:
            _purge_chain_dir(chain_dir, delete_base)
    print(f"RESTORE OK: subvol {args.subvol} -> {args.dest}")
    return 0


# --------------------------------------------------------------------------
# argparse
# --------------------------------------------------------------------------

def _add_verify_flags(p) -> None:
    p.add_argument("--allowed-signers", default=None,
                   help="ssh allowed_signers file pinning the backup key")
    p.add_argument("--identity", default=None,
                   help="signer identity (required with --allowed-signers)")
    p.add_argument("--checkpoint-seq", type=int, default=None,
                   help="owner's recorded monotonic seq (anti-rollback)")
    p.add_argument("--insecure-no-verify", action="store_true",
                   help="skip signature verification (NOT for production)")


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qdistro-backup")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backup", help="encrypt+upload subvols and write a "
                                      "signed manifest")
    b.add_argument("--subvol", action="append", required=True,
                   metavar="NAME:SOURCE[:PARENT]",
                   help="repeatable; one per subvolume")
    b.add_argument("--recipients", required=True)
    b.add_argument("--out-dir", required=True, help="local 'remote' dir")
    b.add_argument("--seq", type=int, required=True)
    b.add_argument("--host-id", default="localhost")
    b.add_argument("--created-at", type=int, default=None)
    b.add_argument("--prev-manifest", default=None,
                   help="previous run's manifest (hash chain + parent blobs)")
    b.add_argument("--sign-key", default=None,
                   help="ssh private key for ssh-keygen -Y sign")
    b.add_argument("--send-cmd", default="btrfs send")
    b.set_defaults(fn=cmd_backup)

    v = sub.add_parser("verify", help="verify manifest chain signature + blobs")
    v.add_argument("--out-dir", required=True)
    _add_verify_flags(v)
    v.set_defaults(fn=cmd_verify)

    r = sub.add_parser("restore", help="verify then receive a subvol chain")
    r.add_argument("--out-dir", required=True)
    r.add_argument("--subvol", required=True)
    r.add_argument("--dest", required=True)
    r.add_argument("--identity-file", required=True,
                   help="rage identity for decryption")
    r.add_argument("--receive-cmd", default="btrfs receive")
    r.add_argument("--subvol-delete-cmd", default="btrfs subvolume delete",
                   help="how to drop intermediate received subvols of an "
                        "incremental chain (injectable for the stub lane)")
    _add_verify_flags(r)
    r.set_defaults(fn=cmd_restore)

    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
