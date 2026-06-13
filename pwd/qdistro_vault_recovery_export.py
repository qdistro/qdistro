"""Owner-side vault-recovery EXPORT tool (06-backup-dr §3.4 / fix F1).

This is the OWNER-DRIVEN producer of the encrypted vault recovery bundle that
the UNATTENDED daily backup collector later folds into its ``recovery/`` subdir
(snapshots/qdistro_backup_recovery.py::validate_recovery_input +
qdistro_backup_service.py::materialise_recovery).

Split of responsibilities (the security model — codex design review):

  - The DAILY service has NO recovery passphrase and never touches the live
    vault master key. It only COPIES an already-exported, ciphertext-only bundle
    through a fail-closed gate.
  - THIS tool is run by the operator, as root, with the vault secret (password
    for a v1 scrypt vault, PIN for a v2 TPM vault) AND a separate owner recovery
    passphrase present. It unlocks the live vault to obtain the 32-byte master
    key IN MEMORY, encrypts it to the recovery passphrase
    (qdistro_vault_recovery.export_recovery_bundle: scrypt + AES-256-GCM), and
    writes the CIPHERTEXT-ONLY bundle to ``--out`` with strict, fail-closed
    output-path handling.

Plaintext-on-disk contract: the master key and the passphrases live ONLY in
process memory; the bytes written to disk are the AEAD ciphertext + public
metadata (version/label/created/kdf params+salt/nonce). NEVER a plaintext
secret. The temp file written during the atomic publish contains the same
encrypted JSON — never plaintext.

Hardening (codex MAJORs):

  1. Output-path safety: the parent dir is opened O_NOFOLLOW and must be a
     real directory owned by root (or a configured owner) and not group/world
     writable. The temp + final are created/renamed RELATIVE TO that dir fd
     (openat/linkat-style via os.open(..., dir_fd=)) with a random O_EXCL 0600
     temp, fsync of file and parent dir, then atomic rename. An existing final
     without ``--force`` is refused; ``--force`` still refuses to follow a
     symlink at the final name.
  2. The recovery passphrase is NOT read from an environment variable by
     default (env secrets leak via proc/env, shell history, child inheritance).
     Default is TTY double-entry; a non-interactive caller may pass an explicit
     ``--passphrase-fd N`` whose contents are read once and cleared.
  3. Verify-before-publish: the freshly built bundle is decrypted in memory
     with the supplied passphrase and the recovered key is compared to the live
     master key BEFORE anything is written — a structurally valid but
     unrecoverable bundle is never shipped.
"""
from __future__ import annotations

import argparse
import getpass
import os
import secrets
import stat
import sys

import qdistro_pwd_vault as vault  # type: ignore[import-not-found]
import qdistro_vault_recovery as rec  # type: ignore[import-not-found]

# Vault dir resolution mirrors the daemon (qdistro_pwd_daemon.VAULT_DIR).
DEFAULT_VAULT_DIR = os.environ.get(
    "QDISTRO_PWD_VAULT_DIR", vault.DEFAULT_VAULT_DIR)


class ExportError(Exception):
    """A fail-closed export error. No partial bundle is ever published."""


# --------------------------------------------------------------------------
# secret input
# --------------------------------------------------------------------------

def _read_passphrase_fd(fd: int) -> bytes:
    """Read the recovery passphrase from an explicit, caller-supplied fd
    (the non-interactive opt-in). One trailing newline is stripped. The fd is
    the caller's responsibility to make root-owned/0600; we never echo it."""
    chunks = []
    try:
        while True:
            b = os.read(fd, 4096)
            if not b:
                break
            chunks.append(b)
    except OSError as e:
        raise ExportError(f"cannot read --passphrase-fd {fd}: {e}") from e
    data = b"".join(chunks)
    if data.endswith(b"\n"):
        data = data[:-1]
    if data.endswith(b"\r"):
        data = data[:-1]
    return data


def _prompt_recovery_passphrase(confirm: bool = True) -> bytes:
    """TTY double-entry of the recovery passphrase (the default path). This is
    a SECOND unlock-everything secret, so we confirm it to avoid a typo that
    would make the bundle permanently un-openable."""
    p1 = getpass.getpass("owner recovery passphrase (guards EVERY silo): ")
    if not confirm:
        return p1.encode("utf-8")
    p2 = getpass.getpass("confirm recovery passphrase: ")
    if p1 != p2:
        raise ExportError("recovery passphrases do not match")
    return p1.encode("utf-8")


def _read_vault_secret(vault_name: str, version: int) -> bytes:
    """The LIVE vault secret used to unlock it for the master key. Mirrors
    qdistro-pwd-admin: env QDISTRO_PWD_PASSWORD for non-interactive use, else a
    single TTY prompt (this secret already exists; no confirm needed)."""
    env = os.environ.get("QDISTRO_PWD_PASSWORD")
    if env is not None:
        return env.encode("utf-8")
    kind = "PIN" if version == vault.VAULT_FORMAT_VERSION_TPM else "password"
    return getpass.getpass(
        f"{kind} for vault {vault_name!r}: ").encode("utf-8")


# --------------------------------------------------------------------------
# master key from the live vault
# --------------------------------------------------------------------------

def _unlock_master_key(vault_dir: str, name: str, secret: bytes) -> bytes:
    """Return the 32-byte master key, dispatching on the vault version.
    Fail-closed: a missing vault, wrong secret, or unavailable TPM backend
    raises (caller exits nonzero, nothing written)."""
    try:
        version = vault.vault_version(vault_dir, name)
    except FileNotFoundError as e:
        raise ExportError(
            f"no vault {name!r} under {vault_dir!r}: {e}") from e
    except Exception as e:
        raise ExportError(f"cannot read vault {name!r}: {e}") from e

    try:
        if version == vault.VAULT_FORMAT_VERSION_TPM:
            # Lazily import the TPM backend lookup only on the v2 path so a
            # host without the TPM module can still export v1 vaults.
            import qdistro_pwd_tpm as tpm  # type: ignore[import-not-found]
            return vault.unlock_vault_tpm(
                vault_dir, name, secret, tpm.lookup_backend)
        return vault.unlock_vault(vault_dir, name, secret)
    except vault.VaultBadPassword as e:
        raise ExportError(
            f"vault {name!r}: wrong vault secret — refusing") from e
    except vault.VaultIntegrityError as e:
        raise ExportError(
            f"vault {name!r}: cannot unlock ({e})") from e
    except ImportError as e:
        raise ExportError(
            f"vault {name!r} is TPM-sealed but the TPM backend is "
            f"unavailable: {e}") from e


# --------------------------------------------------------------------------
# hardened output-path publish
# --------------------------------------------------------------------------

def _check_dir_safe(dir_fd: int, path: str, *,
                     owner_uids: set[int]) -> None:
    """The parent dir (opened O_NOFOLLOW) must be a real directory owned by an
    allowed uid and not group/world-writable — so a recovery-critical bundle
    cannot be planted into an attacker-controllable directory."""
    st = os.fstat(dir_fd)
    if not stat.S_ISDIR(st.st_mode):
        raise ExportError(f"output parent {path!r} is not a directory")
    if st.st_uid not in owner_uids:
        raise ExportError(
            f"output parent {path!r} owned by uid {st.st_uid} "
            f"(expected one of {sorted(owner_uids)}) — refusing")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ExportError(
            f"output parent {path!r} is group/world-writable "
            f"(mode {stat.S_IMODE(st.st_mode):04o}) — refusing")


def _final_exists_nofollow(dir_fd: int, name: str) -> bool:
    """True if a (non-symlink) final target already exists. A symlink at the
    final name is ALWAYS refused (even with --force) so a publish can never be
    redirected through a symlink to overwrite an unrelated file."""
    try:
        st = os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(st.st_mode):
        raise ExportError(
            f"output {name!r} is a symlink — refusing to publish through it")
    return True


def publish_bundle(out_path: str, bundle: dict, *,
                   owner_uids: set[int], force: bool) -> None:
    """Atomically write ``bundle`` (already ciphertext-only) to ``out_path``
    with fail-closed output-path handling. Operations are anchored to a parent
    directory fd opened O_NOFOLLOW so the path cannot be swung under us.

    The temp file is a random O_EXCL 0600 name in the SAME dir (so the rename
    is atomic); we fsync the temp and the parent dir before and after the
    rename so a crash leaves either the old file or the new, never a torn one.
    """
    import json

    parent = os.path.dirname(out_path) or "."
    base = os.path.basename(out_path)
    if not base or base in (".", ".."):
        raise ExportError(f"invalid output path {out_path!r}")

    # Open the parent WITHOUT following a final symlink component of the parent
    # itself; O_DIRECTORY asserts it is a directory.
    try:
        dir_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as e:
        raise ExportError(
            f"cannot open output parent {parent!r} (symlink or missing?): "
            f"{e}") from e
    try:
        _check_dir_safe(dir_fd, parent, owner_uids=owner_uids)
        if _final_exists_nofollow(dir_fd, base):
            if not force:
                raise ExportError(
                    f"output {out_path!r} already exists — pass --force to "
                    "overwrite (the symlink check still applies)")

        body = json.dumps(bundle, indent=2, sort_keys=True).encode("utf-8")
        tmp_name = f".{base}.{secrets.token_hex(8)}.tmp"
        # O_EXCL on a random name: never reuse/unlink a predictable temp.
        tmp_fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                         0o600, dir_fd=dir_fd)
        committed = False
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
            # Atomic replace within the same dir, anchored to the dir fd.
            os.rename(tmp_name, base, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            committed = True
        finally:
            if not committed:
                try:
                    os.unlink(tmp_name, dir_fd=dir_fd)
                except OSError:
                    pass
        # Durability: fsync the parent dir so the rename survives a crash.
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


# --------------------------------------------------------------------------
# the export
# --------------------------------------------------------------------------

def run_export(*, vault_dir: str, vault_name: str, out_path: str,
               label: str, force: bool, owner_uids: set[int],
               vault_secret: bytes, recovery_passphrase: bytes) -> None:
    """End-to-end export: unlock the live vault -> encrypt master key to the
    recovery passphrase -> VERIFY the bundle round-trips -> publish. Any error
    raises ExportError BEFORE publishing (fail closed, no partial bundle)."""
    if not recovery_passphrase:
        raise ExportError("recovery passphrase must not be empty")

    master_key = _unlock_master_key(vault_dir, vault_name, vault_secret)
    try:
        if len(master_key) != rec.MASTER_KEY_BYTES:
            raise ExportError(
                f"unlocked master key has unexpected length {len(master_key)}")
        # Build the ciphertext-only bundle in memory.
        bundle = rec.export_recovery_bundle(
            master_key, recovery_passphrase, label=label)
        # MAJOR 3: verify-before-publish. Decrypt the just-built bundle and
        # confirm it recovers the EXACT live master key. A regression that
        # produced a structurally valid but unrecoverable bundle dies here,
        # before any file is written.
        recovered = rec.decrypt_recovery_bundle(
            bundle, recovery_passphrase, label=label)
        if recovered != master_key:
            raise ExportError(
                "internal error: exported bundle did not round-trip to the "
                "live master key — refusing to publish")
        # Sanity: the recovered copy is also a secret; drop it promptly.
        del recovered
        publish_bundle(out_path, bundle, owner_uids=owner_uids, force=force)
    finally:
        # Best-effort lifetime minimisation (Python bytes cannot be truly
        # zeroed; this just drops our reference).
        del master_key


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def cmd_export(args) -> int:
    owner_uids = {0}
    if args.owner_uid is not None:
        owner_uids.add(int(args.owner_uid))

    # The vault version is needed to phrase the secret prompt correctly; read
    # it first (fail-closed if the vault is missing).
    try:
        version = vault.vault_version(args.vault_dir, args.vault)
    except FileNotFoundError:
        print(f"error: no vault {args.vault!r} under {args.vault_dir!r}",
              file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 — surface any read failure cleanly
        print(f"error: cannot read vault {args.vault!r}: {e}", file=sys.stderr)
        return 2

    vault_secret = _read_vault_secret(args.vault, version)

    if args.passphrase_fd is not None:
        recovery_passphrase = _read_passphrase_fd(args.passphrase_fd)
    else:
        recovery_passphrase = _prompt_recovery_passphrase()

    try:
        run_export(
            vault_dir=args.vault_dir, vault_name=args.vault,
            out_path=args.out, label=args.label, force=args.force,
            owner_uids=owner_uids, vault_secret=vault_secret,
            recovery_passphrase=recovery_passphrase)
    except ExportError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        del vault_secret
        del recovery_passphrase

    print(f"vault recovery bundle written: {args.out}")
    print("CUSTODY: backup ciphertext + this passphrase = every silo. Store "
          "the passphrase like a LUKS recovery key (head/safe), NEVER on this "
          "machine or the backup target.")
    return 0


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qdistro-vault-recovery",
        description="Owner-side vault recovery bundle export (06-backup-dr "
                    "§3.4). Encrypts the vault master key to an owner recovery "
                    "passphrase for non-TPM disaster recovery.")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser(
        "export",
        help="encrypt the vault master key to a recovery passphrase and write "
             "the ciphertext-only bundle")
    e.add_argument(
        "--vault", default="main",
        help="vault name to export (default: main)")
    e.add_argument(
        "--vault-dir", default=DEFAULT_VAULT_DIR, dest="vault_dir",
        help=f"vault directory (default: {DEFAULT_VAULT_DIR})")
    e.add_argument(
        "--out", required=True,
        help="destination bundle path (e.g. "
             "/etc/qdistro/recovery/vault-recovery.json). The parent dir must "
             "be root-owned and not group/world-writable.")
    e.add_argument(
        "--label", default=rec.DEFAULT_LABEL,
        help=f"bundle label, bound into the AEAD (default: {rec.DEFAULT_LABEL}). "
             "Avoid putting vault names / operator identifiers here — the label "
             "is public to anyone holding the backup.")
    e.add_argument(
        "--owner-uid", type=int, default=None, dest="owner_uid",
        help="also accept an output parent dir owned by this uid (in addition "
             "to root). Match the backup [recovery].service_owner_uid.")
    e.add_argument(
        "--passphrase-fd", type=int, default=None, dest="passphrase_fd",
        help="read the recovery passphrase from this fd instead of the TTY "
             "(non-interactive opt-in; the fd must be a root-owned/0600 "
             "source — env-var secrets are intentionally NOT accepted).")
    e.add_argument(
        "--force", action="store_true",
        help="overwrite an existing output file (a symlink is still refused)")
    e.set_defaults(fn=cmd_export)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
