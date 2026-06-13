"""qdistro backup — recovery-bundle collector (06-backup-dr §3.4 / §2c).

Folds the *recovery-critical* material into the config-only metadata COLLECTOR
subvol so a bare-metal restore has everything needed to VERIFY + restore. The
daily backup service is UNATTENDED — it has no recovery passphrase and never
touches the live vault master key — so this module only:

  - COPIES already-exported, NON-SECRET / public-or-encrypted recovery FILES
    (the encrypted vault recovery bundle, the manifest-verification PUBLIC
    allowed_signers, the age PUBLIC recipients) into a ``recovery/`` subdir of
    the collector stage, through a HARD fail-closed validation gate; and
  - GENERATES static documentation from NO secret input (the manifest schema +
    seq-anchor description, a restore runbook, a redacted non-secret echo of
    backup.conf).

Security model (codex design GO, 06 §3.4):

  ALLOWED in the bundle (recovery material, NEVER a secret):
    - encrypted vault recovery bundle: scrypt+AES-256-GCM(master_key) to the
      OWNER recovery passphrase — ciphertext only; the passphrase lives in the
      owner's head/safe. "backup ciphertext + recovery passphrase = all silos",
      so its custody is called out loudly in the generated README.
    - allowed_signers (ssh ed25519 PUBLIC key pinned to an identity) — PUBLIC.
    - age recipients (age1... PUBLIC keys) — PUBLIC.
    - manifest schema / seq-anchor doc, restore runbook, redacted config —
      static, no secret input.

  MUST NEVER (actively refused by validate_recovery_input):
    - the ed25519 PRIVATE signing key (sign_key); the age PRIVATE identity;
      the owner recovery PASSPHRASE; any ssh/age/wg private key; live token DBs.

To prevent an operator MISPOINTING a recovery path at a secret, every ingested
file passes ``validate_recovery_input`` BEFORE it is copied: no symlinks
(O_NOFOLLOW), regular file, owned by root or the service owner, not group/world
writable, no private-key content marker, and a kind-specific PUBLIC-format /
recovery-bundle-schema check. Validation AND copy run against the SAME opened
file descriptor (fstat + read from that fd, then copy that fd) so the file
identity cannot change between check and copy (codex design note: TOCTOU).
"""
from __future__ import annotations

import json
import os
import stat
from typing import Any

# Sniff window: enough to catch a PEM/age private-key header on the first line.
_SNIFF_BYTES = 4096

# Content markers that mean "this is a PRIVATE key/secret" — a hard refuse.
_PRIVATE_MARKERS = (
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
    b"-----BEGIN DSA PRIVATE KEY-----",
    b"-----BEGIN ENCRYPTED PRIVATE KEY-----",
    b"AGE-SECRET-KEY-",
)

# ssh PUBLIC key type tokens accepted in an allowed_signers / recipients line.
_SSH_PUBLIC_KEYTYPES = (
    "ssh-ed25519", "ssh-rsa", "ssh-dss",
    "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com",
)


class RecoveryCollectError(Exception):
    """A recovery-collector input failed its fail-closed validation, or a
    static doc could not be written. Fatal — aborts the backup run rather than
    silently shipping a backup missing (or leaking into) recovery material."""


def _has_private_marker(blob: bytes) -> bool:
    return any(marker in blob for marker in _PRIVATE_MARKERS)


def _check_allowed_signers(text: str) -> None:
    """Every non-blank, non-comment line must look like an ssh allowed_signers
    PUBLIC entry: ``<principals> <keytype> <base64...>`` with a public keytype.
    Reject anything carrying a private marker."""
    saw = False
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        saw = True
        parts = s.split()
        # principal(s) keytype base64[ comment...]  -> >= 3 fields, a public type
        if len(parts) < 3 or not any(kt in parts for kt in _SSH_PUBLIC_KEYTYPES):
            raise RecoveryCollectError(
                "allowed_signers line is not a recognised PUBLIC ssh signer "
                f"entry: {s[:60]!r}")
    if not saw:
        raise RecoveryCollectError("allowed_signers file has no signer entries")


def _check_recipients(text: str) -> None:
    """Every non-blank, non-comment line must be an age PUBLIC recipient
    (``age1...``) or an ssh PUBLIC key line. Reject a private marker."""
    saw = False
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        saw = True
        if s.startswith("age1"):
            continue
        parts = s.split()
        if parts and parts[0] in _SSH_PUBLIC_KEYTYPES:
            continue
        raise RecoveryCollectError(
            f"recipients line is not a PUBLIC age/ssh recipient: {s[:60]!r}")
    if not saw:
        raise RecoveryCollectError("recipients file has no recipients")


def _check_recovery_bundle(text: str) -> None:
    """Structurally validate the file IS a qdistro vault recovery bundle
    (06-backup-dr §3.4 / qdistro_vault_recovery export shape) — not decrypted
    (no passphrase here), just proven to be the bundle and not some other file
    mis-pointed at this slot."""
    try:
        obj = json.loads(text)
    except (ValueError, TypeError) as e:
        raise RecoveryCollectError(
            f"recovery bundle is not valid JSON: {e}") from e
    if not isinstance(obj, dict):
        raise RecoveryCollectError("recovery bundle is not a JSON object")
    if obj.get("version") != 1:
        raise RecoveryCollectError(
            f"unsupported recovery bundle version {obj.get('version')!r}")
    kdf = obj.get("kdf")
    aead = obj.get("aead")
    if not isinstance(kdf, dict) or kdf.get("alg") != "scrypt" \
            or not all(k in kdf for k in ("n", "r", "p", "salt")):
        raise RecoveryCollectError(
            "recovery bundle kdf section is not the expected scrypt shape")
    if not isinstance(aead, dict) or aead.get("alg") != "AES-256-GCM" \
            or not all(k in aead for k in ("nonce", "ciphertext")):
        raise RecoveryCollectError(
            "recovery bundle aead section is not the expected AES-256-GCM shape")
    # A bundle with an empty salt/nonce/ciphertext is structurally "valid" JSON
    # but useless recovery material (it could never decrypt to a master key) —
    # reject it so a hollowed-out bundle can't be shipped as the escape hatch.
    if not (isinstance(kdf.get("salt"), str) and kdf["salt"]):
        raise RecoveryCollectError("recovery bundle kdf.salt is empty")
    if not (isinstance(aead.get("nonce"), str) and aead["nonce"]):
        raise RecoveryCollectError("recovery bundle aead.nonce is empty")
    if not (isinstance(aead.get("ciphertext"), str) and aead["ciphertext"]):
        raise RecoveryCollectError("recovery bundle aead.ciphertext is empty")


_KIND_CHECKERS = {
    "allowed_signers": _check_allowed_signers,
    "recipients": _check_recipients,
    "recovery_bundle": _check_recovery_bundle,
}


def validate_recovery_input(path: str, kind: str, *,
                            service_owner_uid: int | None = None) -> bytes:
    """Fail-closed validation of ONE recovery-collector input. Returns the file
    BYTES (read from the validated fd) on success; raises RecoveryCollectError
    on any problem. The caller writes exactly these returned bytes — so the
    bytes validated and the bytes stored are provably the same (no TOCTOU).

    ``kind`` is one of: allowed_signers, recipients, recovery_bundle.
    A file is REFUSED unless ALL hold:
      - it is opened WITHOUT following a symlink (O_NOFOLLOW);
      - it is a regular file;
      - owned by root(0) or ``service_owner_uid`` (if given);
      - not group- or world-writable;
      - its content carries NO private-key marker;
      - it passes the kind-specific PUBLIC-format / bundle-schema check.
    """
    if kind not in _KIND_CHECKERS:
        raise RecoveryCollectError(f"unknown recovery input kind {kind!r}")
    try:
        # O_NOFOLLOW: a symlink at `path` raises ELOOP rather than opening its
        # target — the anti-symlink-to-secret defence, atomic with the open.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        raise RecoveryCollectError(
            f"recovery input {path!r} ({kind}): cannot open (symlink or "
            f"missing?): {e}") from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RecoveryCollectError(
                f"recovery input {path!r} is not a regular file")
        allowed_uids = {0}
        if service_owner_uid is not None:
            allowed_uids.add(int(service_owner_uid))
        if st.st_uid not in allowed_uids:
            raise RecoveryCollectError(
                f"recovery input {path!r} owned by uid {st.st_uid} "
                f"(expected one of {sorted(allowed_uids)}) — refusing")
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise RecoveryCollectError(
                f"recovery input {path!r} is group/world-writable "
                f"(mode {stat.S_IMODE(st.st_mode):04o}) — refusing")
        # Read the WHOLE file from the validated fd; this is both the content
        # we sniff/parse AND the content we return for copying (no second open).
        data = b""
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)

    if _has_private_marker(data[:_SNIFF_BYTES]) or _has_private_marker(data):
        raise RecoveryCollectError(
            f"recovery input {path!r} ({kind}) contains a PRIVATE-key marker "
            "— refusing to put a secret in the backup")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise RecoveryCollectError(
            f"recovery input {path!r} ({kind}) is not UTF-8 text: {e}") from e
    _KIND_CHECKERS[kind](text)
    return data


# --------------------------------------------------------------------------
# static documentation (generated from NO secret input)
# --------------------------------------------------------------------------

MANIFEST_SCHEMA_DOC = """\
qdistro backup — manifest schema + seq anchor (06-backup-dr §3.1/§3.2)

Each backup run publishes, next to its encrypted blobs:
  manifest-<seq>.json      canonical JSON, the run's authenticated record
  manifest-<seq>.json.sig  ssh-keygen -Y ed25519 signature over the canonical
                           bytes (namespace "qdistro-backup")

Manifest shape:
  {
    "version": 1,
    "seq": <int>,                 monotonic, owner-visible run counter
    "created_at": <unix int>,
    "host_id": "<this machine>",  a restore refuses to mix host_ids
    "entries": [
      { "subvol": "<name>",
        "blob": "<name>-<seq>.btrfs.age",
        "sha256": "<64 hex>",
        "size": <bytes>,
        "parent_blob": "<earlier blob>" | null }   null == a full send
    ],
    "prev_manifest_sha256": "<sha256 of the previous manifest's canonical
                             bytes>" | null         null only for seq 0
  }

What each layer buys (be precise):
  - signature  -> authenticity + anti-substitution (NOT anti-replay)
  - hash chain (prev_manifest_sha256) + strictly increasing seq + gapless
    per-subvol parent_blob lineage -> a dropped/substituted run is detected
  - FRESHNESS  -> ONLY the owner-side monotonic checkpoint defeats a rollback
    to an older, validly-signed set. Record the newest seq OFF this machine
    (printed card / phone / password manager) and pass it as the restore's
    --checkpoint-seq. Without that external record, rollback is possible.

The on-machine anchor: /var/lib/qdistro/backup/state.json holds {seq, host_id,
subvols}, plus a local manifests/ copy of each manifest. The daily service is
the chain authority at backup time; the weekly rehearsal cross-checks the
remote against this anchor (remote seq must be >= local seq). The on-machine
anchor is NOT a substitute for the off-machine DR checkpoint above.
"""

# NOTE: the restore runbook MUST make the trust direction explicit — the
# allowed_signers copy collected here is RESTORED DOCUMENTATION ONLY, never the
# DR trust root (codex MAJOR 4).
RESTORE_RUNBOOK_DOC = """\
qdistro backup — restore runbook pointers (06-backup-dr §3.5)

TRUST DIRECTION FIRST (read this before anything else):
  The allowed_signers and recipients files in THIS recovery/ directory are
  RESTORED DOCUMENTATION ONLY. They came off the SAME backup target that served
  you the blobs, so they cannot be the trust root. Verify the manifest chain
  with the manifest-verification PUBLIC key you hold OFF this machine (printed
  card / password manager / second device); use these copies only to re-create
  the on-disk pin AFTER that off-machine material has accepted the chain.

Bare-metal restore outline:
  1. Install the base OS; restore network.
  2. Fetch the newest manifest + .sig from the backup target. Verify the
     signature with the OFF-MACHINE public key, then `qdistro-backup verify`
     the chain. Check the newest seq against your OWN externally-recorded
     value (anti-rollback) and pass it as --checkpoint-seq.
  3. Run qdistro-bootstrap (idempotent).
  4. Restore the metadata/collector subvol first (this set), reconcile.
  5. `qdistro-backup restore` each silo subvolume across its full chain
     (needs the age PRIVATE identity you keep OFF this machine).
  6. VAULT: decrypt the recovery bundle (recovery/vault-recovery.json) with the
     OWNER RECOVERY PASSPHRASE (qdistro vault-recovery), then re-seal into the
     new machine's TPM. The passphrase lives in your head/safe — it is NOT in
     this backup. CUSTODY: this backup's ciphertext PLUS that passphrase unlocks
     every silo; guard the passphrase like a LUKS recovery key.
  7. Rebuild derived templates from recipes; per-silo health checks.

Files in this recovery/ directory:
  manifest-schema.txt     the manifest format + seq/freshness rules
  config-redacted.txt     non-secret layout echo of backup.conf (NO secrets)
  allowed_signers         PUBLIC signer pin (documentation only — see above)
  recipients              PUBLIC age recipients (documentation only)
  vault-recovery.json     ENCRYPTED vault recovery bundle (needs the passphrase)
"""


def redacted_config_text(cfg: dict[str, Any]) -> str:
    """A non-secret echo of the layout so a restorer knows what to fetch and
    where. OMITS sign_key ENTIRELY (its path is omitted too — codex MAJOR 4),
    and never echoes secret contents. recipients/allowed_signers are PATHS to
    public material; echoing the path is fine."""
    lines = [
        "qdistro backup — redacted config (non-secret layout echo)",
        "# sign_key and any private-key path are intentionally OMITTED.",
        "",
        f"host_id  = {cfg.get('host_id')!r}",
        f"remote   = {cfg.get('remote')!r}",
        f"recipients_path     = {cfg.get('recipients')!r}  # PUBLIC age keys",
    ]
    if cfg.get("sign_identity"):
        lines.append(f"sign_identity       = {cfg.get('sign_identity')!r}")
    if cfg.get("allowed_signers"):
        lines.append(
            f"allowed_signers_path = {cfg.get('allowed_signers')!r}  # PUBLIC")
    lines.append("")
    lines.append("subvols:")
    for sv in cfg.get("subvols", []):
        if sv.get("collector"):
            lines.append(f"  - {sv['name']}  (collector: {sv.get('paths')})")
        else:
            lines.append(f"  - {sv['name']}  (source: {sv.get('source')})")
    return "\n".join(lines) + "\n"
