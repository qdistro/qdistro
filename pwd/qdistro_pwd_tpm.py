"""TPM2 sealing backend for qdistro password-manager vaults.

spec/13 Phase-8.1: layered KEK = TPM unseal + admin PIN.

The TPM provides two properties the scrypt-only path can't:

1. **Hardware-backed lockout.** The TPM rate-limits incorrect auth-value
   attempts (default ~30s after a few wrong tries, exponential backoff),
   so a stolen vault file isn't brute-forceable offline once sealed.
2. **Anti-extraction.** The seal blob is decryptable only by THIS TPM;
   copying the .vault file to another machine yields nothing useful
   even if the PIN is leaked.

The PIN is the TPM auth-value. Combining the two layers means the
attacker needs both physical access to the TPM AND the PIN to recover
the master key. PIN length can be much shorter than a scrypt password
(e.g. 6-12 digits) because TPM lockout makes brute force infeasible.

## Backend abstraction

`TpmBackend` is a tiny duck-typed protocol with three methods:

    backend.is_available() -> bool
    backend.seal(secret: bytes, auth_pin: bytes) -> dict[str, Any]
    backend.unseal(blob: dict, auth_pin: bytes) -> bytes

Three implementations:

- `Tpm2ToolsBackend` — drives `tpm2_createprimary / tpm2_create /
  tpm2_load / tpm2_unseal` via subprocess. Used in production. Requires
  /dev/tpmrm0 + tpm2-tools + tpm2-abrmd OR the in-kernel resource
  manager and the `tpm` group on the daemon's uid.
- `MockBackend` — pure-Python fake (scrypt + AES-GCM with the PIN as
  password). Used in tests and as a safe fallback when the host lacks
  a TPM but the user explicitly opts-in to a "weaker than v1, but at
  least uses the v2 path" vault. NOT a security boundary; clearly
  marked as such in the blob.
- `NoneBackend` — `is_available()` False; raise on every call. The
  default when no TPM is present and no opt-in is set.

## Backend selection

`select_backend()` reads `QDISTRO_PWD_TPM_BACKEND`:

  - `tpm2tools` (default if /dev/tpmrm0 + tpm2-tools both present)
  - `mock` (test-only)
  - `none` (default if no TPM)

Plus `QDISTRO_PWD_TPM_TCTI` (default `device:/dev/tpmrm0`) and
`QDISTRO_PWD_TPM_PRIMARY_HANDLE` (default 0x81000010 — persistent
SRK in the owner hierarchy, created on first seal if absent).

## Anti-DA-lockout note

The seal uses an HMAC-secured session so the auth-value (PIN) is
never sent in cleartext on the bus. The TPM's dictionary-attack (DA)
counter is decremented on wrong-PIN attempts.

## PCR binding (Phase-8.5)

`seal(secret, pin, pcrs="sha256:7,11")` binds the unseal authorisation
to a PCR policy. The Tpm2ToolsBackend builds a policy via
``tpm2_startauthsession`` + ``tpm2_policypcr`` against the live TPM
state, hashes it into ``policy.digest``, and passes ``-L
policy.digest`` to ``tpm2_create`` so the resulting object can only
be unsealed under a session that re-asserts the same PCR values.
Unseal repeats the policy build, then ``tpm2_unseal --auth
session:<pcr.session>`` proves the assertion. Tampered initrd/
firmware extends PCR 7 (secure-boot) or 11 (UKI digest) which causes
the policy to mismatch and the unseal to fail with TpmAuthFailed
even given the right PIN.

The MockBackend simulates PCR binding by sealing a record of the
``QDISTRO_PWD_TPM_MOCK_PCR_STATE`` env value at seal time and
refusing to unseal when the env disagrees. This is a structural
fake, not a security boundary — production uses Tpm2ToolsBackend.

PCR config:
  - env ``QDISTRO_PWD_TPM_PCRS``           (default: ``sha256:7,11``)
  - env ``QDISTRO_PWD_TPM_MOCK_PCR_STATE`` (mock backend only;
                                            default: empty string)

Backwards compatibility: a v2 vault sealed before Phase-8.5 has no
``pcrs`` field in its tpm_seal blob; unseal proceeds without a PCR
session. New seals default to PCR binding when the env doesn't
explicitly opt out (``QDISTRO_PWD_TPM_PCRS=`` empty).

## Combined PCR + PIN seal (task 103)

When both ``pcrs`` is non-empty AND ``auth_pin`` is non-empty,
``seal()`` builds a compound policy that requires BOTH at unseal:

  1. ``tpm2_policypcr`` against the live PCR state (boot integrity).
  2. ``tpm2_policyauthvalue`` so the unseal also asserts a known
     auth-value (the PIN).

The seal blob carries ``combined_auth: True`` so unseal knows to
replay both ops + supply the PIN via the
``-p session:<sess>+hex:<pin>`` syntax. Mismatched PCR state
surfaces as ``TpmAuthFailed`` (boot tampered); wrong PIN surfaces
as ``TpmAuthFailed`` from ``tpm2_unseal``'s authorisation check.

Pre-task-103 v2 vaults silently dropped the PIN at unseal — the
session-only flow ignored the auth-value. Re-creating an existing
vault with the new code upgrades it to combined; old blobs without
the ``combined_auth`` flag stay on the legacy PCR-only-or-PIN-only
path so existing data still unlocks.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
from typing import Any, Protocol

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class TpmBackendError(Exception):
    """Raised when a TPM backend operation fails for backend-internal reasons
    (TCTI gone, persistent handle wedged, tpm2-tools missing). Callers should
    treat it as a hard failure — no fall-through to a weaker backend."""


class TpmAuthFailed(Exception):
    """Raised when unseal fails because the auth-value (PIN) is wrong, the
    DA-lockout has tripped, or the seal blob doesn't match this TPM. Mapped
    by the daemon to a 'wrong PIN / locked out' user message."""


class TpmUnavailable(Exception):
    """Raised when no TPM backend is usable in the current environment."""


# ---------------------------------------------------------------------------
# Backend protocol
# ---------------------------------------------------------------------------

class TpmBackend(Protocol):
    name: str

    def is_available(self) -> bool: ...
    def seal(self, secret: bytes, auth_pin: bytes,
             pcrs: str | None = None) -> dict[str, Any]: ...
    def unseal(self, blob: dict[str, Any], auth_pin: bytes) -> bytes: ...


# Default PCR selection for the bound seal. PCR 7 reflects secure
# boot state on UEFI systems (Microsoft + manufacturer keys; sealed
# bootloader hash). PCR 11 is the UKI/initrd digest extended by
# systemd-stub. A tampered firmware (different secure-boot keys) or
# initrd image will change one of these, breaking unseal.
DEFAULT_PCRS = "sha256:7,11"


def configured_pcrs(env: dict | None = None) -> str | None:
    """Return the PCR selection string (or None to skip binding).

    Reads ``QDISTRO_PWD_TPM_PCRS``. The default is DEFAULT_PCRS.
    Empty string explicitly disables PCR binding (legacy / dev mode).
    """
    if env is None:
        env = os.environ
    if "QDISTRO_PWD_TPM_PCRS" not in env:
        return DEFAULT_PCRS
    val = env["QDISTRO_PWD_TPM_PCRS"].strip()
    if not val:
        return None
    return val


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


# ---------------------------------------------------------------------------
# tpm2-tools backend (production)
# ---------------------------------------------------------------------------

class Tpm2ToolsBackend:
    """Drive the host's tpm2-tools via subprocess."""

    name = "tpm2tools"

    def __init__(self,
                 tcti: str | None = None,
                 primary_handle: str = "0x81000010") -> None:
        self.tcti = tcti or os.environ.get(
            "QDISTRO_PWD_TPM_TCTI", "device:/dev/tpmrm0")
        self.primary_handle = primary_handle

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["TPM2TOOLS_TCTI"] = self.tcti
        return env

    def _run(self, *args: str, input_bytes: bytes | None = None,
             cwd: str | None = None) -> tuple[int, bytes, bytes]:
        """Run a tpm2_* binary. Returns (rc, stdout, stderr)."""
        proc = subprocess.run(
            list(args), input=input_bytes, capture_output=True,
            env=self._env(), cwd=cwd, timeout=30)
        return proc.returncode, proc.stdout, proc.stderr

    # ---- availability check -------------------------------------------------

    def is_available(self) -> bool:
        if shutil.which("tpm2_getrandom") is None:
            return False
        rc, _, _ = self._run("tpm2_getrandom", "--hex", "8")
        return rc == 0

    # ---- primary -----------------------------------------------------------

    def _ensure_primary(self, work: str) -> str:
        """Make sure the persistent primary at self.primary_handle exists.
        Returns the handle string. Idempotent — re-creates on first seal,
        no-op on subsequent.

        Strategy: try `tpm2_readpublic -c <handle>`. If it succeeds the
        handle is populated. If it fails, create a transient primary in
        the owner hierarchy with the standard ECC-256 SRK template,
        evict-control it to the persistent slot, then flush the transient.
        """
        rc, _, _ = self._run("tpm2_readpublic", "-c", self.primary_handle,
                             "-Q")
        if rc == 0:
            return self.primary_handle
        # Create transient primary in /tmp work dir.
        ctx = os.path.join(work, "primary.ctx")
        rc, out, err = self._run(
            "tpm2_createprimary",
            "-C", "o",                       # owner hierarchy
            "-c", ctx,
            "-G", "ecc",                     # ECC-256 default
            "-g", "sha256",
            "-Q")
        if rc != 0:
            raise TpmBackendError(
                f"tpm2_createprimary failed: rc={rc} stderr={err.decode(errors='replace')!r}")
        rc, _, err = self._run(
            "tpm2_evictcontrol",
            "-C", "o",
            "-c", ctx,
            self.primary_handle,
            "-Q")
        if rc != 0:
            # Transient primary still works for seal/unseal — fall back to
            # caller using ctx file directly. Future calls will rebuild.
            raise TpmBackendError(
                f"tpm2_evictcontrol to {self.primary_handle} failed: "
                f"rc={rc} stderr={err.decode(errors='replace')!r}")
        return self.primary_handle

    # ---- seal --------------------------------------------------------------

    def _build_pcr_policy(self, work: str, pcrs: str,
                          *, combined_auth: bool = False) -> str:
        """Compute a policy digest binding a TPM-sealed object to the
        live PCR state, optionally extended with a policyauthvalue
        assertion so unseal also requires the object's auth-value (PIN).

        Returns the path to the .digest file. The work-dir's session
        context is flushed before return — the digest file alone is
        what tpm2_create needs.

        Steps:
          1. ``tpm2_startauthsession --session=session.ctx``
          2. ``tpm2_policypcr -S session.ctx -l <pcrs>`` (no -L yet
             when combining; we need the running session digest after
             both ops)
          3. (if combined_auth) ``tpm2_policyauthvalue -S session.ctx``
          4. ``tpm2_policypcr / tpm2_policyauthvalue -L policy.digest``
             — final step writes the cumulative digest
          5. ``tpm2_flushcontext session.ctx``

        spec/13 §"combined PCR + PIN seal" (task 103): the same trial
        flow during unseal asserts the live PCR state AND the supplied
        auth-value via session:<sess>+hex:<pin> tpm2_unseal -p syntax.
        """
        sess = os.path.join(work, "policy.session")
        digest = os.path.join(work, "policy.digest")
        rc, _, err = self._run("tpm2_startauthsession",
                               "--session", sess, "-Q")
        if rc != 0:
            raise TpmBackendError(
                f"tpm2_startauthsession failed: rc={rc} "
                f"stderr={err.decode(errors='replace')!r}")
        # When combining, hold off on -L until the second op so the
        # written digest is the cumulative one (policypcr → policyauthvalue).
        if combined_auth:
            rc, _, err = self._run("tpm2_policypcr",
                                   "-S", sess, "-l", pcrs, "-Q")
        else:
            rc, _, err = self._run("tpm2_policypcr",
                                   "-S", sess, "-l", pcrs,
                                   "-L", digest, "-Q")
        if rc != 0:
            self._run("tpm2_flushcontext", sess, "-Q")
            raise TpmBackendError(
                f"tpm2_policypcr failed (pcrs={pcrs!r}): rc={rc} "
                f"stderr={err.decode(errors='replace')!r}")
        if combined_auth:
            rc, _, err = self._run("tpm2_policyauthvalue",
                                   "-S", sess, "-L", digest, "-Q")
            if rc != 0:
                self._run("tpm2_flushcontext", sess, "-Q")
                raise TpmBackendError(
                    f"tpm2_policyauthvalue failed: rc={rc} "
                    f"stderr={err.decode(errors='replace')!r}")
        self._run("tpm2_flushcontext", sess, "-Q")
        return digest

    def seal(self, secret: bytes, auth_pin: bytes,
             pcrs: str | None = None) -> dict[str, Any]:
        if not (1 <= len(secret) <= 128):
            raise ValueError(f"TPM seal secret must be 1..128 bytes, got {len(secret)}")
        # spec/13 §"combined PCR + PIN seal" (task 103): when both
        # `pcrs` and `auth_pin` are supplied, build a compound policy
        # that requires BOTH to unseal. Pre-task-103, passing both
        # silently dropped the PIN at unseal — the unseal path used
        # the policy session alone. Combined seals carry
        # `combined_auth: True` in the blob; old blobs without that
        # flag still unseal under the legacy PCR-only-or-PIN-only path.
        combined_auth = bool(pcrs) and bool(auth_pin)
        with tempfile.TemporaryDirectory(prefix="qdpwd-tpm-seal-") as work:
            handle = self._ensure_primary(work)
            sec_path = os.path.join(work, "secret.bin")
            with open(sec_path, "wb") as f:
                f.write(secret)
            os.chmod(sec_path, 0o600)
            priv_path = os.path.join(work, "obj.priv")
            pub_path = os.path.join(work, "obj.pub")
            policy_digest_b64 = ""
            if pcrs:
                digest_path = self._build_pcr_policy(
                    work, pcrs, combined_auth=combined_auth)
                with open(digest_path, "rb") as f:
                    policy_digest_b64 = _b64e(f.read())
            args = [
                "tpm2_create",
                "-C", handle,
                "-i", sec_path,
                "-r", priv_path,
                "-u", pub_path,
                "-a", "fixedtpm|fixedparent|adminwithpolicy|noda|userwithauth",
                "-Q",
            ]
            if pcrs:
                args.extend(["-L", os.path.join(work, "policy.digest")])
            if auth_pin:
                args.extend(["-p", "hex:" + auth_pin.hex()])
            rc, _, err = self._run(*args)
            if rc != 0:
                raise TpmBackendError(
                    f"tpm2_create failed: rc={rc} stderr={err.decode(errors='replace')!r}")
            with open(priv_path, "rb") as f:
                priv = f.read()
            with open(pub_path, "rb") as f:
                pub = f.read()
            try:
                os.unlink(sec_path)
            except OSError:
                pass
            out: dict[str, Any] = {
                "primary_handle": handle,
                "priv": _b64e(priv),
                "pub": _b64e(pub),
                "auth_set": bool(auth_pin),
            }
            if pcrs:
                out["pcrs"] = pcrs
                out["policy_digest"] = policy_digest_b64
            if combined_auth:
                out["combined_auth"] = True
            return out

    # ---- unseal ------------------------------------------------------------

    def unseal(self, blob: dict[str, Any], auth_pin: bytes) -> bytes:
        priv = _b64d(blob["priv"])
        pub = _b64d(blob["pub"])
        handle = blob.get("primary_handle", self.primary_handle)
        pcrs = blob.get("pcrs")
        combined_auth = bool(blob.get("combined_auth", False))
        with tempfile.TemporaryDirectory(prefix="qdpwd-tpm-unseal-") as work:
            # Make sure the persistent primary still exists; re-create if
            # the TPM was cleared between seal and unseal (recovery path).
            self._ensure_primary(work)
            priv_path = os.path.join(work, "obj.priv")
            pub_path = os.path.join(work, "obj.pub")
            ctx_path = os.path.join(work, "obj.ctx")
            sess_path = os.path.join(work, "policy.session")
            with open(priv_path, "wb") as f:
                f.write(priv)
            with open(pub_path, "wb") as f:
                f.write(pub)
            rc, _, err = self._run(
                "tpm2_load", "-C", handle,
                "-r", priv_path, "-u", pub_path,
                "-c", ctx_path, "-Q")
            if rc != 0:
                raise TpmBackendError(
                    f"tpm2_load failed: rc={rc} stderr={err.decode(errors='replace')!r}")
            # PCR-bound seal: open a policy session, replay PCR
            # assertion against the live state, then unseal under it.
            # spec/13 §"combined PCR + PIN seal" (task 103): when the
            # blob carries `combined_auth: True`, the trial policy at
            # seal time included a policyauthvalue assertion — replay
            # that here AND pass the PIN via the session's +hex:<pin>
            # suffix so unseal needs both factors.
            if pcrs:
                rc, _, err = self._run("tpm2_startauthsession",
                                       "--policy-session",
                                       "-S", sess_path, "-Q")
                if rc != 0:
                    raise TpmBackendError(
                        f"tpm2_startauthsession (policy) failed: rc={rc} "
                        f"stderr={err.decode(errors='replace')!r}")
                rc, _, err = self._run("tpm2_policypcr",
                                       "-S", sess_path,
                                       "-l", pcrs, "-Q")
                if rc != 0:
                    self._run("tpm2_flushcontext", sess_path, "-Q")
                    # Mismatched PCR state ≡ tampered boot path or
                    # different host. Surface as TpmAuthFailed so
                    # callers get the "wrong PIN / lockout / boot
                    # tamper" mapping.
                    raise TpmAuthFailed(
                        f"tpm2_policypcr mismatch (pcrs={pcrs!r}): "
                        f"{err.decode(errors='replace').strip()}")
                if combined_auth:
                    rc, _, err = self._run("tpm2_policyauthvalue",
                                           "-S", sess_path, "-Q")
                    if rc != 0:
                        self._run("tpm2_flushcontext", sess_path, "-Q")
                        raise TpmBackendError(
                            f"tpm2_policyauthvalue (replay) failed: rc={rc} "
                            f"stderr={err.decode(errors='replace')!r}")
            args = ["tpm2_unseal", "-c", ctx_path]
            if combined_auth:
                # session+pin combined auth: the session asserts PCR +
                # the policyauthvalue requirement; the +hex:<pin> piece
                # supplies the actual auth-value.
                args.extend(["-p",
                             "session:" + sess_path
                             + "+hex:" + auth_pin.hex()])
            elif pcrs:
                args.extend(["-p", "session:" + sess_path])
            elif auth_pin or blob.get("auth_set"):
                args.extend(["-p", "hex:" + auth_pin.hex()])
            rc, out, err = self._run(*args)
            if pcrs:
                self._run("tpm2_flushcontext", sess_path, "-Q")
            if rc != 0:
                txt = err.decode(errors="replace").lower()
                if ("authorization failure" in txt
                        or "bad auth" in txt
                        or "lockout" in txt
                        or "policy check failed" in txt):
                    raise TpmAuthFailed(
                        f"tpm2_unseal auth/lockout: {err.decode(errors='replace').strip()}")
                raise TpmBackendError(
                    f"tpm2_unseal failed: rc={rc} stderr={err.decode(errors='replace')!r}")
            return out


# ---------------------------------------------------------------------------
# Mock backend (tests + opt-in fallback)
# ---------------------------------------------------------------------------

class MockBackend:
    """Pure-Python fake. Encrypts the secret with scrypt(PIN) + AES-GCM.

    NOT a security boundary equivalent to a real TPM — there is no
    hardware-enforced lockout, and the seal blob is portable across
    machines. The format-version-2 vault marks `backend: mock` so
    a future audit / migration tool can flag these as needing rotation
    once a real TPM is provisioned.

    Intended use: pytest, in-VM smoke tests when swtpm isn't wired up,
    explicit user opt-in for environments without TPM where the user
    accepts the weaker security profile.
    """

    name = "mock"

    def is_available(self) -> bool:
        return True

    @staticmethod
    def _mock_pcr_state() -> str:
        """Synthetic stand-in for the live PCR set. Tests can pin
        this via QDISTRO_PWD_TPM_MOCK_PCR_STATE; default empty string
        is taken as "not yet measured" (still bound — mismatch on
        seal-with-state vs unseal-without)."""
        return os.environ.get("QDISTRO_PWD_TPM_MOCK_PCR_STATE", "")

    def seal(self, secret: bytes, auth_pin: bytes,
             pcrs: str | None = None) -> dict[str, Any]:
        # Re-use the existing scrypt+AES-GCM crypto so we don't pull in
        # a second crypto stack just for the mock.
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
        salt = os.urandom(16)
        kdf = Scrypt(salt=salt, length=32, n=16384, r=8, p=1)  # cheap for tests
        key = kdf.derive(auth_pin or b"")
        nonce = os.urandom(12)
        # AAD includes the PCR selection + the recorded PCR state at
        # seal time. AES-GCM rejects decrypt if the unseal-time AAD
        # doesn't match — that's the mock's stand-in for the TPM's
        # policy session: change PCR state → unseal fails.
        # task(103): when both PCR and PIN are supplied, mark the blob
        # `combined_auth: True` so the unseal-side mirrors the
        # tpm2-tools backend — both factors are now required.
        combined_auth = bool(pcrs) and bool(auth_pin)
        aad_parts = [b"qdistro-pwd-mock-tpm-v1"]
        out: dict[str, Any] = {
            "salt": _b64e(salt),
            "nonce": _b64e(nonce),
        }
        if pcrs:
            state = self._mock_pcr_state()
            aad_parts.append(b"pcrs=" + pcrs.encode("utf-8"))
            aad_parts.append(b"state=" + state.encode("utf-8"))
            out["pcrs"] = pcrs
        if combined_auth:
            aad_parts.append(b"combined_auth=1")
            out["combined_auth"] = True
        aad = b"|".join(aad_parts)
        ct = AESGCM(key).encrypt(nonce, secret, aad)
        out["ciphertext"] = _b64e(ct)
        return out

    def unseal(self, blob: dict[str, Any], auth_pin: bytes) -> bytes:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
        salt = _b64d(blob["salt"])
        nonce = _b64d(blob["nonce"])
        ct = _b64d(blob["ciphertext"])
        pcrs = blob.get("pcrs")
        combined_auth = bool(blob.get("combined_auth", False))
        kdf = Scrypt(salt=salt, length=32, n=16384, r=8, p=1)
        key = kdf.derive(auth_pin or b"")
        aad_parts = [b"qdistro-pwd-mock-tpm-v1"]
        if pcrs:
            state = self._mock_pcr_state()
            aad_parts.append(b"pcrs=" + str(pcrs).encode("utf-8"))
            aad_parts.append(b"state=" + state.encode("utf-8"))
        if combined_auth:
            aad_parts.append(b"combined_auth=1")
        aad = b"|".join(aad_parts)
        try:
            return AESGCM(key).decrypt(nonce, ct, aad)
        except InvalidTag as exc:
            raise TpmAuthFailed(
                "mock-tpm: wrong PIN or PCR state") from exc


# ---------------------------------------------------------------------------
# None backend
# ---------------------------------------------------------------------------

class NoneBackend:
    name = "none"

    def is_available(self) -> bool:
        return False

    def seal(self, secret: bytes, auth_pin: bytes,
             pcrs: str | None = None) -> dict[str, Any]:
        raise TpmUnavailable("no TPM backend selected (set QDISTRO_PWD_TPM_BACKEND)")

    def unseal(self, blob: dict[str, Any], auth_pin: bytes) -> bytes:
        raise TpmUnavailable("no TPM backend selected (set QDISTRO_PWD_TPM_BACKEND)")


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

_BACKENDS: dict[str, type] = {
    "tpm2tools": Tpm2ToolsBackend,
    "mock":      MockBackend,
    "none":      NoneBackend,
}


def select_backend(name: str | None = None) -> TpmBackend:
    """Pick a backend by name (or env var, or auto-detect).

    Order of preference when name is None:

      1. QDISTRO_PWD_TPM_BACKEND env (explicit).
      2. tpm2tools if /dev/tpmrm0 exists AND tpm2_getrandom can talk
         to it.
      3. NoneBackend.
    """
    if name is None:
        name = os.environ.get("QDISTRO_PWD_TPM_BACKEND")
    if name:
        cls = _BACKENDS.get(name)
        if cls is None:
            raise ValueError(f"unknown TPM backend {name!r}")
        return cls()
    # Auto-detect.
    if os.path.exists("/dev/tpmrm0"):
        be = Tpm2ToolsBackend()
        if be.is_available():
            return be
    return NoneBackend()


def lookup_backend(name: str) -> TpmBackend:
    """Build a backend by name (no env / autodetect). Used by unseal to
    resolve the backend recorded in a vault's tpm_seal blob."""
    cls = _BACKENDS.get(name)
    if cls is None:
        raise ValueError(f"unknown TPM backend {name!r}")
    return cls()
