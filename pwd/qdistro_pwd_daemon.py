#!/usr/bin/env python3
"""qdistro password-manager daemon.

System-bus D-Bus service mediating access to encrypted vaults. Phase 8
MVP slice (spec/13): single password-encrypted vault per name, layered
caller-identity verification, per-item app-pin gate. Defers TPM,
fingerprint, recovery codes, multi-vault policy DSL, autotype, browser
bridge, SSH agent, portal.Secret backend.

Daemon runs as the dedicated `qdistro-pwd` system uid (created by
fresh-vm-bootstrap.sh / install-pwd-for-vm.sh). The vault directory
/var/lib/qdistro/vaults/ is owned 0700 by qdistro-pwd; only the daemon
domain can read .vault files (enforced by the SELinux module
qdistro_pwd.{te,fc} once shipped).

Bus name: `org.qdistro.Pwd1` on the system bus, object `/org/qdistro/Pwd1`.

Method matrix (admin uid = 1000):

    Admin-only:
      CreateVault(name, password) -> bool
      AddItem(vault, tag, value, pin_app_exe, pin_selinux, pin_uid) -> bool
      DeleteItem(vault, tag) -> bool
      ListItems(vault) -> aa{sv}
      GetItemAdmin(vault, tag) -> string         # bypasses app-pin gate
      ListAuditLog(limit) -> aa{sv}

    Any caller:
      ListVaults() -> as
      IsUnlocked(vault) -> bool
      UnlockVault(vault, password) -> bool       # admin-prompt-equivalent for MVP
      LockVault(vault) -> bool
      GetItem(vault, tag) -> string              # gated by per-item pin match

Audit log: every Get attempt (allow / deny), every Unlock / Lock /
admin-write op. Payload is never persisted.

Auto-lock: vaults relock after IDLE_TIMEOUT_S of no activity (default
10 minutes). Configurable via QDISTRO_PWD_IDLE_S env var.
"""
from __future__ import annotations

import os
import signal
import sys
import time
from typing import Any

import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib

from qdistro_pwd_vault import (  # type: ignore[import-not-found]
    DEFAULT_VAULT_DIR, VAULT_FORMAT_VERSION_TPM,
    VaultBadPassword, VaultDuplicate, VaultIntegrityError, VaultNotFound,
    add_item, create_vault, create_vault_tpm, delete_item, get_item_payload,
    get_item_pins, get_tpm_seal_meta, list_items, list_vaults, rotate_vault,
    rotate_vault_tpm, unlock_vault, unlock_vault_tpm, vault_version,
)
from qdistro_pwd_identity import (  # type: ignore[import-not-found]
    snapshot_caller, pin_match,
)
from qdistro_pwd_audit import PwdAuditLog  # type: ignore[import-not-found]
from qdistro_pwd_tpm import (  # type: ignore[import-not-found]
    TpmUnavailable, configured_pcrs, lookup_backend, select_backend,
)
from qdistro_pwd_polkit import (  # type: ignore[import-not-found]
    PolkitDenied, PolkitNoAgent, check_unlock as polkit_check_unlock,
)
from qdistro_pwd_pinstash import (  # type: ignore[import-not-found]
    DEFAULT_STASH_PATH as PORTAL_PIN_STASH_PATH,
    MAX_PIN_BYTES as PORTAL_PIN_MAX_BYTES,
    PinStashError, stash_meta as portal_pin_meta,
    stash_pin as portal_pin_stash, unseal_pin as portal_pin_unseal,
)
from qdistro_pwd_fprint import (  # type: ignore[import-not-found]
    admin_username as fprint_admin_username,
    is_fprintd_available as fprint_is_available,
    verify as fprint_verify,
)

BUS_NAME = "org.qdistro.Pwd1"
OBJ_PATH = "/org/qdistro/Pwd1"
ADMIN_UID = 1000

VAULT_DIR = os.environ.get("QDISTRO_PWD_VAULT_DIR", DEFAULT_VAULT_DIR)
AUDIT_DB = os.environ.get(
    "QDISTRO_PWD_AUDIT_DB", "/var/lib/qdistro/audit/pwd_audit.sqlite")

# Vault used by the spec/13 Phase-8.3 XDG portal Secret backend to
# stash per-app-id master keys. Defaults to "portal-keys"; admin
# creates this vault before the portal backend can serve secrets.
PORTAL_KEYS_VAULT = os.environ.get("QDISTRO_PWD_PORTAL_VAULT",
                                   "portal-keys")
PORTAL_KEY_BYTES = 32  # length of bytes returned to portal callers

# Optional second-factor: gate AutoUnlockPortalKeys on a successful
# fprintd verify before unsealing the stashed PIN. Off by default so
# hosts without a fingerprint reader keep working unmodified. Set to
# "1" / "true" / "yes" to enable. When enabled and fprintd is absent
# OR no finger enrolled, the daemon hard-fails — operator must either
# enroll a finger or unset the env to recover. Set
# QDISTRO_PORTAL_FPRINT_OPTIONAL=1 alongside to soft-skip when fprintd
# is unreachable (useful for mixed fleets).
PORTAL_REQUIRE_FPRINT = os.environ.get(
    "QDISTRO_PORTAL_REQUIRE_FPRINT", "0").lower() in ("1", "true", "yes")
PORTAL_FPRINT_OPTIONAL = os.environ.get(
    "QDISTRO_PORTAL_FPRINT_OPTIONAL", "0").lower() in ("1", "true", "yes")

# Vault auto-lock idle timeout. After this many seconds with no GetItem
# / IsUnlocked / list activity, the in-memory master key is wiped.
IDLE_TIMEOUT_S = int(os.environ.get("QDISTRO_PWD_IDLE_S", "600"))


class PwdPolicyError(dbus.DBusException):
    _dbus_error_name = "org.qdistro.Pwd1.PolicyError"


class PwdNotUnlocked(dbus.DBusException):
    _dbus_error_name = "org.qdistro.Pwd1.NotUnlocked"


class PwdBadPassword(dbus.DBusException):
    _dbus_error_name = "org.qdistro.Pwd1.BadPassword"


class PwdNotFound(dbus.DBusException):
    _dbus_error_name = "org.qdistro.Pwd1.NotFound"


class PwdDuplicate(dbus.DBusException):
    _dbus_error_name = "org.qdistro.Pwd1.Duplicate"


class PwdIntegrityError(dbus.DBusException):
    _dbus_error_name = "org.qdistro.Pwd1.Integrity"


def _wipe_bytearray(b: bytearray) -> None:
    """Best-effort overwrite of a sensitive byte buffer."""
    try:
        for i in range(len(b)):
            b[i] = 0
    except Exception:
        pass


class PwdDaemon(dbus.service.Object):
    def __init__(self, bus):
        super().__init__(bus, OBJ_PATH)
        # vault name → {"key": bytearray, "unlocked_at": ts, "last_use": ts}
        self._unlocked: dict[str, dict[str, Any]] = {}
        self._audit = PwdAuditLog(AUDIT_DB)
        # Idle tick: every 30s, relock vaults that have been idle for
        # more than IDLE_TIMEOUT_S.
        GLib.timeout_add_seconds(30, self._idle_tick)

    # -- helpers -------------------------------------------------------

    def _peer_info(self, sender: str) -> tuple[int, int]:
        bus = dbus.SystemBus()
        dbus_proxy = bus.get_object(
            "org.freedesktop.DBus", "/org/freedesktop/DBus")
        ifc = dbus.Interface(dbus_proxy, "org.freedesktop.DBus")
        uid = int(ifc.GetConnectionUnixUser(sender))
        pid = int(ifc.GetConnectionUnixProcessID(sender))
        return uid, pid

    def _require_admin(self, sender: str) -> None:
        uid, _ = self._peer_info(sender)
        if uid != ADMIN_UID:
            raise PwdPolicyError(
                f"operation requires admin uid {ADMIN_UID}, got {uid}")

    def _touch(self, vault: str) -> None:
        if vault in self._unlocked:
            self._unlocked[vault]["last_use"] = int(time.time())

    def _idle_tick(self) -> bool:
        now = int(time.time())
        relocked = []
        for name, state in list(self._unlocked.items()):
            if now - state["last_use"] >= IDLE_TIMEOUT_S:
                self._do_lock(name, reason="idle-timeout")
                relocked.append(name)
        for name in relocked:
            self.VaultLocked(name, "idle-timeout")
        return True  # reschedule

    def _do_lock(self, name: str, *, reason: str) -> bool:
        state = self._unlocked.pop(name, None)
        if state is None:
            return False
        key = state.get("key")
        if isinstance(key, bytearray):
            _wipe_bytearray(key)
        self._audit.record(
            "lock", name, decision="allow", reason=reason)
        return True

    # -- vault lifecycle -----------------------------------------------

    @dbus.service.method(BUS_NAME, in_signature="ss", out_signature="b",
                         sender_keyword="sender")
    def CreateVault(self, name: str, password: str, sender=None) -> bool:
        self._require_admin(sender)
        try:
            create_vault(VAULT_DIR, str(name), str(password).encode("utf-8"))
        except VaultDuplicate as e:
            self._audit.record("create", str(name),
                               decision="deny", reason=str(e))
            raise PwdDuplicate(str(e))
        except ValueError as e:
            raise PwdPolicyError(str(e))
        self._audit.record("create", str(name), decision="allow",
                           reason="vault created (v1/scrypt)")
        return True

    @dbus.service.method(BUS_NAME, in_signature="ss", out_signature="b",
                         sender_keyword="sender")
    def CreateVaultTPM(self, name: str, pin: str, sender=None) -> bool:
        """Create a v2 TPM-sealed vault. PIN becomes the TPM auth-value
        (anti-DA-lockout enforced by the TPM). Backend selected per the
        QDISTRO_PWD_TPM_BACKEND env on the daemon."""
        self._require_admin(sender)
        backend = select_backend()
        if not backend.is_available():
            raise PwdPolicyError(
                f"TPM backend {backend.name!r} not available on this host")
        try:
            create_vault_tpm(VAULT_DIR, str(name),
                             str(pin).encode("utf-8"), backend,
                             pcrs=configured_pcrs())
        except VaultDuplicate as e:
            self._audit.record("create", str(name),
                               decision="deny", reason=str(e))
            raise PwdDuplicate(str(e))
        except ValueError as e:
            raise PwdPolicyError(str(e))
        except TpmUnavailable as e:
            raise PwdPolicyError(f"TPM unavailable: {e}")
        self._audit.record("create", str(name), decision="allow",
                           reason=f"vault created (v2/tpm:{backend.name})")
        return True

    @dbus.service.method(BUS_NAME, out_signature="as")
    def ListVaults(self) -> list[str]:
        return list_vaults(VAULT_DIR)

    @dbus.service.method(BUS_NAME, in_signature="s", out_signature="i")
    def VaultVersion(self, name: str) -> int:
        """Return the on-disk format version (1 = scrypt, 2 = TPM-sealed)
        so a CLI can route an UnlockVault call to the right secret kind
        (password vs PIN)."""
        try:
            return vault_version(VAULT_DIR, str(name))
        except VaultNotFound as e:
            raise PwdNotFound(str(e))

    @dbus.service.method(BUS_NAME, in_signature="s", out_signature="a{sv}")
    def VaultInfo(self, name: str) -> dict:
        """Return a compact metadata blob: version, tpm backend (if v2)."""
        try:
            v = vault_version(VAULT_DIR, str(name))
        except VaultNotFound as e:
            raise PwdNotFound(str(e))
        out: dict = {
            "version":     dbus.Int32(v),
            "tpm_backend": dbus.String(""),
            "tpm_pcrs":    dbus.String(""),
        }
        if v == VAULT_FORMAT_VERSION_TPM:
            seal = get_tpm_seal_meta(VAULT_DIR, str(name))
            out["tpm_backend"] = dbus.String(seal.get("backend", ""))
            out["tpm_pcrs"]    = dbus.String(seal.get("pcrs", ""))
        return out

    @dbus.service.method(BUS_NAME, in_signature="s", out_signature="b")
    def IsUnlocked(self, name: str) -> bool:
        return str(name) in self._unlocked

    @dbus.service.method(BUS_NAME, in_signature="ss", out_signature="b",
                         sender_keyword="sender")
    def UnlockVault(self, name: str, secret: str, sender=None) -> bool:
        """Unlock a vault. Dispatches on the vault's on-disk version:
        v1 → secret is the scrypt password; v2 → secret is the TPM PIN.

        For non-admin callers, gated through the polkit action
        `org.qdistro.pwd.unlock` so an admin polkit agent prompts
        before any secret is unsealed. Admin uid bypasses polkit
        (caller is already authoritative).
        """
        uid, pid = self._peer_info(sender)
        caller = snapshot_caller(pid, uid)
        name = str(name)
        # Polkit gate — admin uid bypasses; non-admin must clear polkit
        # auth via a registered AuthenticationAgent before we even touch
        # the disk. This is the spec/13 Phase-8.2 entry point.
        try:
            allowed, polkit_reason = polkit_check_unlock(
                uid, pid, name, caller_exe=caller.get("exe", ""))
        except PolkitNoAgent as e:
            self._audit.record("unlock", name, decision="deny",
                               reason="polkit-no-agent", caller=caller)
            raise PwdPolicyError(
                f"polkit gate: no admin AuthenticationAgent registered "
                f"({e})") from e
        except PolkitDenied as e:
            self._audit.record("unlock", name, decision="deny",
                               reason="polkit-denied", caller=caller)
            raise PwdPolicyError(f"polkit gate refused: {e}") from e
        except dbus.DBusException as e:
            self._audit.record("unlock", name, decision="deny",
                               reason=f"polkit-dbus:{e.get_dbus_name()}",
                               caller=caller)
            raise PwdPolicyError(
                f"polkit gate unreachable: {e.get_dbus_message()}") from e
        if not allowed:
            self._audit.record("unlock", name, decision="deny",
                               reason=f"polkit:{polkit_reason}", caller=caller)
            raise PwdPolicyError(f"polkit gate refused: {polkit_reason}")
        try:
            v = vault_version(VAULT_DIR, name)
        except VaultNotFound as e:
            self._audit.record("unlock", name, decision="deny",
                               reason="no-such-vault", caller=caller)
            raise PwdNotFound(str(e)) from e
        try:
            if v == VAULT_FORMAT_VERSION_TPM:
                key = unlock_vault_tpm(VAULT_DIR, name,
                                       str(secret).encode("utf-8"),
                                       lookup_backend)
                unlock_reason = f"pin-ok-tpm/{polkit_reason}"
            else:
                key = unlock_vault(VAULT_DIR, name,
                                   str(secret).encode("utf-8"))
                unlock_reason = f"password-ok/{polkit_reason}"
        except VaultBadPassword as e:
            self._audit.record("unlock", name, decision="deny",
                               reason="bad-password", caller=caller)
            raise PwdBadPassword(str(e)) from e
        except VaultIntegrityError as e:
            self._audit.record("unlock", name, decision="deny",
                               reason=f"integrity:{e}", caller=caller)
            raise PwdIntegrityError(str(e)) from e
        # Stash the key in a bytearray so we can zero it on lock. The
        # original `bytes` is immutable; Python doesn't expose a way to
        # zero it, so the best we can do is hold the only mutable copy.
        ba = bytearray(key)
        now = int(time.time())
        self._unlocked[name] = {"key": ba, "unlocked_at": now, "last_use": now}
        self._audit.record("unlock", name, decision="allow",
                           reason=unlock_reason, caller=caller)
        self.VaultUnlocked(name)
        return True

    @dbus.service.method(BUS_NAME, in_signature="ss", out_signature="b",
                         sender_keyword="sender")
    def UnlockVaultFprint(self, name: str, secret: str,
                          sender=None) -> bool:
        """Admin-only fast-path: fprintd verify + unseal in one
        round-trip.

        Same shape as UnlockVault (vault name + secret) but skips the
        polkit gate in favour of a direct fprintd VerifyStart cycle.
        Useful for app-driven dialogs that want to surface "Touch
        sensor" themselves rather than going through the polkit agent's
        prompt subprocess.

        Caller MUST be admin uid (fprintd is bound to a specific
        Linux user; cross-uid fprintd verification is intentionally
        unsupported here). The PIN/password is supplied by the caller —
        fprintd alone cannot derive the vault's master key, so this
        method is not a stand-alone factor; it's a faster *gate* than
        polkit while the actual unseal still runs through the v1/v2
        crypto path.

        On fprintd unreachable / no enrolled finger, raises
        ``PwdPolicyError`` (fail-closed). Wrong fingerprint raises
        ``PwdBadPassword`` so the caller's UI can offer a re-touch
        without distinguishing "no match" from "wrong PIN".
        """
        uid, pid = self._peer_info(sender)
        if uid != ADMIN_UID:
            raise PwdPolicyError(
                f"UnlockVaultFprint requires admin uid {ADMIN_UID}, "
                f"got {uid}")
        caller = snapshot_caller(pid, uid)
        name = str(name)
        sysbus = dbus.SystemBus()
        user = fprint_admin_username(ADMIN_UID)
        if not fprint_is_available(sysbus):
            self._audit.record("unlock", name, decision="deny",
                               reason="fprint-unavailable", caller=caller)
            raise PwdPolicyError(
                "fprintd unreachable or no enrolled fingerprint")
        matched, fpr_reason = fprint_verify(user, sysbus)
        if not matched:
            self._audit.record("unlock", name, decision="deny",
                               reason=f"fprint-deny:{fpr_reason}",
                               caller=caller)
            raise PwdBadPassword(
                f"fingerprint verification failed: {fpr_reason}")
        try:
            v = vault_version(VAULT_DIR, name)
        except VaultNotFound as e:
            self._audit.record("unlock", name, decision="deny",
                               reason="no-such-vault", caller=caller)
            raise PwdNotFound(str(e)) from e
        try:
            if v == VAULT_FORMAT_VERSION_TPM:
                key = unlock_vault_tpm(VAULT_DIR, name,
                                       str(secret).encode("utf-8"),
                                       lookup_backend)
                kind = "v2"
            else:
                key = unlock_vault(VAULT_DIR, name,
                                   str(secret).encode("utf-8"))
                kind = "v1"
        except VaultBadPassword as e:
            self._audit.record("unlock", name, decision="deny",
                               reason="bad-password", caller=caller)
            raise PwdBadPassword(str(e)) from e
        except VaultIntegrityError as e:
            self._audit.record("unlock", name, decision="deny",
                               reason=f"integrity:{e}", caller=caller)
            raise PwdIntegrityError(str(e)) from e
        ba = bytearray(key)
        now = int(time.time())
        self._unlocked[name] = {"key": ba, "unlocked_at": now,
                                "last_use": now}
        self._audit.record(
            "unlock", name, decision="allow",
            reason=f"fprint-pass:{fpr_reason}/{kind}",
            caller=caller)
        self.VaultUnlocked(name)
        return True

    @dbus.service.method(BUS_NAME, in_signature="s", out_signature="b",
                         sender_keyword="sender")
    def LockVault(self, name: str, sender=None) -> bool:
        uid, _ = self._peer_info(sender)
        # Locking is not gated on admin; any caller can lock (anyone can
        # ALWAYS lock more, just not unlock more — symmetric with file-
        # system unlinking-vs-creating).
        ok = self._do_lock(str(name), reason=f"explicit-lock-uid{uid}")
        if ok:
            self.VaultLocked(str(name), "explicit")
        return ok

    @dbus.service.method(BUS_NAME, in_signature="sss", out_signature="b",
                         sender_keyword="sender")
    def RotateVault(self, name: str, old_secret: str, new_secret: str,
                    sender=None) -> bool:
        """Rotate a vault's secret without re-encrypting items.

        Dispatches on the vault's on-disk version: v1 → password
        rotation (new salt + re-derived KEK); v2 → PIN rotation (re-seal
        master key under new PIN, also picks up the current PCR state).
        Items remain byte-for-byte unchanged because they are encrypted
        under the master key, not the password/PIN.
        Admin uid only.
        """
        self._require_admin(sender)
        uid, pid = self._peer_info(sender)
        caller = snapshot_caller(pid, uid)
        name = str(name)
        # Lock the in-memory copy (if any) before rotating — the master
        # key bytes don't change but downstream callers should re-unlock
        # under the new secret to reset the idle timer's lineage.
        was_unlocked = name in self._unlocked
        try:
            v = vault_version(VAULT_DIR, name)
        except VaultNotFound as e:
            self._audit.record("rotate", name, decision="deny",
                               reason="no-such-vault", caller=caller)
            raise PwdNotFound(str(e)) from e
        try:
            if v == VAULT_FORMAT_VERSION_TPM:
                backend = select_backend()
                if not backend.is_available():
                    raise PwdPolicyError(
                        f"TPM backend {backend.name!r} not available")
                rotate_vault_tpm(VAULT_DIR, name,
                                 str(old_secret).encode("utf-8"),
                                 str(new_secret).encode("utf-8"),
                                 backend, lookup_backend,
                                 pcrs=configured_pcrs())
                rotate_reason = f"pin-rotated-tpm/{backend.name}"
            else:
                rotate_vault(VAULT_DIR, name,
                             str(old_secret).encode("utf-8"),
                             str(new_secret).encode("utf-8"))
                rotate_reason = "password-rotated-scrypt"
        except VaultBadPassword as e:
            self._audit.record("rotate", name, decision="deny",
                               reason="bad-old-secret", caller=caller)
            raise PwdBadPassword(str(e)) from e
        except VaultIntegrityError as e:
            self._audit.record("rotate", name, decision="deny",
                               reason=f"integrity:{e}", caller=caller)
            raise PwdIntegrityError(str(e)) from e
        except TpmUnavailable as e:
            self._audit.record("rotate", name, decision="deny",
                               reason=f"tpm-unavailable:{e}", caller=caller)
            raise PwdPolicyError(f"TPM unavailable: {e}") from e
        if was_unlocked:
            # Force a fresh unlock under the new secret. The master key
            # bytes are unchanged but caller should re-prove the new
            # secret before further item access.
            self._do_lock(name, reason="rotated")
            self.VaultLocked(name, "rotated")
        self._audit.record("rotate", name, decision="allow",
                           reason=rotate_reason, caller=caller)
        return True

    # -- item management (admin) ---------------------------------------

    @dbus.service.method(BUS_NAME,
                         in_signature="ssssss",
                         out_signature="b",
                         sender_keyword="sender")
    def AddItem(self, vault: str, tag: str, value: str,
                pin_app_exe: str, pin_selinux: str, pin_uid: str = "",
                sender=None) -> bool:
        # Signature note: pin_uid is wire-typed as a string (D-Bus 's'),
        # not an int32 ('i'), because every documented caller passes
        # six strings (qdistro/pwd/README.md §"AddItem"). The empty
        # string is treated as "no uid pin" (-1 internally); any
        # non-empty value is parsed as a decimal int. A previous
        # iteration declared ``sssssis`` with a trailing reserved
        # string, which made the busctl/test call paths impossible to
        # write without a mid-signature int — see s105 in qdistro2
        # tests/integration/vm/.
        self._require_admin(sender)
        vault = str(vault)
        tag = str(tag)
        if vault not in self._unlocked:
            raise PwdNotUnlocked(f"vault {vault!r} is locked")
        self._touch(vault)
        pin_uid_s = str(pin_uid).strip()
        pin_uid_i = int(pin_uid_s) if pin_uid_s else -1
        try:
            add_item(VAULT_DIR, vault,
                     bytes(self._unlocked[vault]["key"]),
                     tag, str(value).encode("utf-8"),
                     pin_app_exe=str(pin_app_exe),
                     pin_selinux=str(pin_selinux),
                     pin_uid=(pin_uid_i if pin_uid_i >= 0 else None),
                     replace=True)
        except (VaultDuplicate, VaultNotFound, ValueError) as e:
            self._audit.record("add", vault, item_tag=tag,
                               decision="error", reason=str(e))
            raise PwdPolicyError(str(e))
        self._audit.record("add", vault, item_tag=tag,
                           decision="allow", reason="item upserted")
        return True

    @dbus.service.method(BUS_NAME, in_signature="ss", out_signature="b",
                         sender_keyword="sender")
    def DeleteItem(self, vault: str, tag: str, sender=None) -> bool:
        self._require_admin(sender)
        vault = str(vault)
        tag = str(tag)
        try:
            ok = delete_item(VAULT_DIR, vault, tag)
        except VaultNotFound as e:
            raise PwdNotFound(str(e))
        self._audit.record("delete", vault, item_tag=tag,
                           decision="allow" if ok else "error",
                           reason="deleted" if ok else "absent")
        return ok

    @dbus.service.method(BUS_NAME, in_signature="s", out_signature="aa{sv}",
                         sender_keyword="sender")
    def ListItems(self, vault: str, sender=None) -> list[dict]:
        self._require_admin(sender)
        try:
            return [self._wrap_item_meta(it) for it in list_items(VAULT_DIR, str(vault))]
        except VaultNotFound as e:
            raise PwdNotFound(str(e))

    @staticmethod
    def _wrap_item_meta(it: dict[str, Any]) -> dict[str, Any]:
        return {
            "tag":         dbus.String(it.get("tag", "")),
            "pin_app_exe": dbus.String(it.get("pin_app_exe", "")),
            "pin_selinux": dbus.String(it.get("pin_selinux", "")),
            "pin_uid":     dbus.Int32(int(it["pin_uid"]) if it.get("pin_uid") is not None else -1),
            "created":     dbus.Int64(int(it.get("created", 0))),
        }

    # -- get item (the load-bearing path) ------------------------------

    @dbus.service.method(BUS_NAME, in_signature="ss", out_signature="s",
                         sender_keyword="sender")
    def GetItem(self, vault: str, tag: str, sender=None) -> str:
        """Read an item — gated by per-item app-pin match against the
        kernel-attested caller identity. Admin uid bypasses the pin
        gate via GetItemAdmin.
        """
        uid, pid = self._peer_info(sender)
        caller = snapshot_caller(pid, uid)
        vault = str(vault)
        tag = str(tag)
        if vault not in self._unlocked:
            self._audit.record("get", vault, item_tag=tag,
                               decision="deny", reason="vault-locked",
                               caller=caller)
            raise PwdNotUnlocked(f"vault {vault!r} is locked")
        self._touch(vault)
        try:
            pins = get_item_pins(VAULT_DIR, vault, tag)
        except VaultNotFound as e:
            self._audit.record("get", vault, item_tag=tag,
                               decision="deny", reason="no-such-item",
                               caller=caller)
            raise PwdNotFound(str(e))
        ok, reason = pin_match(pins, caller)
        if not ok:
            self._audit.record("get", vault, item_tag=tag,
                               decision="deny", reason=reason,
                               caller=caller)
            raise PwdPolicyError(f"pin gate refused: {reason}")
        try:
            payload = get_item_payload(
                VAULT_DIR, vault, bytes(self._unlocked[vault]["key"]), tag)
        except VaultIntegrityError as e:
            self._audit.record("get", vault, item_tag=tag,
                               decision="deny", reason="integrity-fail",
                               caller=caller)
            raise PwdIntegrityError(str(e))
        self._audit.record("get", vault, item_tag=tag,
                           decision="allow", reason=reason, caller=caller)
        return payload.decode("utf-8")

    @dbus.service.method(BUS_NAME, in_signature="ss", out_signature="s",
                         sender_keyword="sender")
    def GetItemAdmin(self, vault: str, tag: str, sender=None) -> str:
        """Admin-only retrieval that bypasses the per-item pin gate. Used
        by the admin CLI for inspection and recovery."""
        self._require_admin(sender)
        uid, pid = self._peer_info(sender)
        caller = snapshot_caller(pid, uid)
        vault = str(vault)
        tag = str(tag)
        if vault not in self._unlocked:
            raise PwdNotUnlocked(f"vault {vault!r} is locked")
        self._touch(vault)
        try:
            payload = get_item_payload(
                VAULT_DIR, vault, bytes(self._unlocked[vault]["key"]), tag)
        except VaultNotFound as e:
            raise PwdNotFound(str(e))
        except VaultIntegrityError as e:
            raise PwdIntegrityError(str(e))
        self._audit.record("get-admin", vault, item_tag=tag,
                           decision="allow", reason="admin-bypass",
                           caller=caller)
        return payload.decode("utf-8")

    # -- portal-keys PIN stash + auto-unlock (spec/13 §"auto-unlock") ---
    #
    # Admin sets the portal-keys vault PIN once via
    # `qdistro-pwd-admin store-portal-pin`; the daemon TPM-seals it
    # and writes the sealed blob to /var/lib/qdistro/portal-keys-pin.tpm.
    # A session systemd unit calls AutoUnlockPortalKeys at login so
    # unmodified Flatpak apps can immediately fetch portal Secret keys.

    @dbus.service.method(BUS_NAME, in_signature="ay", out_signature="b",
                         sender_keyword="sender")
    def StashPortalPin(self, pin, sender=None) -> bool:
        """Admin-only: TPM-seal the portal-keys vault PIN onto disk for
        later auto-unlock."""
        self._require_admin(sender)
        pin_bytes = bytes(bytearray(int(b) for b in pin))
        if not pin_bytes:
            raise PwdPolicyError("portal-keys PIN must be non-empty")
        if len(pin_bytes) > PORTAL_PIN_MAX_BYTES:
            raise PwdPolicyError(
                f"portal-keys PIN too long ({len(pin_bytes)} bytes; "
                f"max {PORTAL_PIN_MAX_BYTES})")
        backend = select_backend()
        if not backend.is_available():
            raise PwdPolicyError(
                f"TPM backend {backend.name!r} not available — "
                f"portal-keys PIN stash needs a working TPM")
        try:
            meta = portal_pin_stash(pin_bytes, backend,
                                    path=PORTAL_PIN_STASH_PATH)
        except (TpmUnavailable, ValueError) as e:
            raise PwdPolicyError(f"PIN stash failed: {e}") from e
        self._audit.record("portal-pin-stash", PORTAL_KEYS_VAULT,
                           decision="allow",
                           reason=f"sealed via {meta['backend']}")
        return True

    @dbus.service.method(BUS_NAME, out_signature="a{sv}",
                         sender_keyword="sender")
    def PortalPinStashInfo(self, sender=None) -> dict:
        """Admin-only: return stash metadata (no unseal)."""
        self._require_admin(sender)
        meta = portal_pin_meta(PORTAL_PIN_STASH_PATH)
        return {
            "present":         dbus.Boolean(bool(meta["present"])),
            "backend":         dbus.String(meta["backend"]),
            "created_at_unix": dbus.Int64(int(meta["created_at_unix"])),
            "stash_path":      dbus.String(PORTAL_PIN_STASH_PATH),
        }

    @dbus.service.method(BUS_NAME, out_signature="b",
                         sender_keyword="sender")
    def AutoUnlockPortalKeys(self, sender=None) -> bool:
        """Admin-only: unseal the stashed PIN + unlock the portal-keys
        vault. Idempotent — returns True if already unlocked.

        When ``QDISTRO_PORTAL_REQUIRE_FPRINT=1``, requires a fprintd
        verify cycle (touch the sensor) BEFORE unsealing the PIN. This
        shifts the load-bearing factor from "stashed PIN alone" to
        "stashed PIN + live presence", at the cost of needing a touch
        at every login. Set ``QDISTRO_PORTAL_FPRINT_OPTIONAL=1`` to
        soft-skip when fprintd is unreachable (mixed-fleet recovery).
        """
        self._require_admin(sender)
        if PORTAL_KEYS_VAULT in self._unlocked:
            return True
        if PORTAL_REQUIRE_FPRINT:
            sysbus = dbus.SystemBus()
            user = fprint_admin_username(ADMIN_UID)
            if not fprint_is_available(sysbus):
                if PORTAL_FPRINT_OPTIONAL:
                    self._audit.record(
                        "portal-auto-unlock", PORTAL_KEYS_VAULT,
                        decision="allow",
                        reason="fprint-skip:fprintd-unreachable")
                else:
                    self._audit.record(
                        "portal-auto-unlock", PORTAL_KEYS_VAULT,
                        decision="deny",
                        reason="fprint-required:fprintd-unreachable")
                    raise PwdPolicyError(
                        "fprintd unreachable but "
                        "QDISTRO_PORTAL_REQUIRE_FPRINT=1")
            else:
                matched, fpr_reason = fprint_verify(user, sysbus)
                if not matched:
                    self._audit.record(
                        "portal-auto-unlock", PORTAL_KEYS_VAULT,
                        decision="deny",
                        reason=f"fprint-deny:{fpr_reason}")
                    raise PwdBadPassword(
                        f"fingerprint verification failed: {fpr_reason}")
                self._audit.record(
                    "portal-auto-unlock", PORTAL_KEYS_VAULT,
                    decision="allow",
                    reason=f"fprint-pass:{fpr_reason}")
        try:
            pin = portal_pin_unseal(lookup_backend,
                                    path=PORTAL_PIN_STASH_PATH)
        except PinStashError as e:
            self._audit.record("portal-auto-unlock", PORTAL_KEYS_VAULT,
                               decision="deny", reason=f"stash:{e}")
            raise PwdNotFound(str(e)) from e
        except TpmAuthFailed as e:
            self._audit.record("portal-auto-unlock", PORTAL_KEYS_VAULT,
                               decision="deny", reason=f"tpm-auth:{e}")
            raise PwdBadPassword(str(e)) from e
        except TpmBackendError as e:
            self._audit.record("portal-auto-unlock", PORTAL_KEYS_VAULT,
                               decision="deny", reason=f"tpm-error:{e}")
            raise PwdIntegrityError(str(e)) from e
        try:
            v = vault_version(VAULT_DIR, PORTAL_KEYS_VAULT)
            if v == VAULT_FORMAT_VERSION_TPM:
                key = unlock_vault_tpm(VAULT_DIR, PORTAL_KEYS_VAULT,
                                       pin, lookup_backend)
                kind = "v2"
            else:
                key = unlock_vault(VAULT_DIR, PORTAL_KEYS_VAULT, pin)
                kind = "v1"
        except VaultNotFound as e:
            self._audit.record("portal-auto-unlock", PORTAL_KEYS_VAULT,
                               decision="deny", reason="no-such-vault")
            raise PwdNotFound(str(e)) from e
        except VaultBadPassword as e:
            self._audit.record("portal-auto-unlock", PORTAL_KEYS_VAULT,
                               decision="deny", reason="stale-pin")
            raise PwdBadPassword(str(e)) from e
        except VaultIntegrityError as e:
            self._audit.record("portal-auto-unlock", PORTAL_KEYS_VAULT,
                               decision="deny", reason=f"integrity:{e}")
            raise PwdIntegrityError(str(e)) from e
        finally:
            # Always wipe the unsealed PIN bytes — only the master key
            # lives on past this point.
            ba_pin = bytearray(pin)
            _wipe_bytearray(ba_pin)
        ba = bytearray(key)
        now = int(time.time())
        self._unlocked[PORTAL_KEYS_VAULT] = {
            "key": ba, "unlocked_at": now, "last_use": now,
        }
        self._audit.record("portal-auto-unlock", PORTAL_KEYS_VAULT,
                           decision="allow",
                           reason=f"auto-unsealed {kind}")
        self.VaultUnlocked(PORTAL_KEYS_VAULT)
        return True

    # -- portal Secret backend bridge (spec/13 Phase-8.3) ---------------

    @dbus.service.method(BUS_NAME, in_signature="s", out_signature="ay",
                         sender_keyword="sender")
    def GetPortalKey(self, app_id: str, sender=None):
        """Return the per-app-id portal Secret key as bytes.

        Drives the org.freedesktop.impl.portal.Secret.RetrieveSecret
        flow. The portal backend daemon (running per-user in the
        session) calls this method on the system bus; we look up
        `portal/<app_id>` in the configured PORTAL_KEYS_VAULT, auto-
        provisioning a 32-byte random key on first use.

        Caller identity (uid/pid + exe via /proc) is captured for the
        audit log. The key is delivered to the per-user portal daemon
        which writes it to the fd the app handed in.

        Constraints:
          - The portal-keys vault MUST be unlocked first (admin task).
          - Caller must be a non-admin uid (admin doesn't need portal
            keys; this endpoint is for the per-user portal backend).
            App-id format is loosely validated (no path separators,
            non-empty) — the portal frontend already validated it
            against flatpak-info, but we double-check.
        """
        uid, pid = self._peer_info(sender)
        caller = snapshot_caller(pid, uid)
        app_id = str(app_id)
        if not app_id or "/" in app_id or app_id.startswith("."):
            self._audit.record("portal-get", PORTAL_KEYS_VAULT,
                               item_tag=app_id, decision="deny",
                               reason="bad-app-id", caller=caller)
            raise PwdPolicyError(f"invalid app_id: {app_id!r}")
        if PORTAL_KEYS_VAULT not in self._unlocked:
            self._audit.record("portal-get", PORTAL_KEYS_VAULT,
                               item_tag=app_id, decision="deny",
                               reason="vault-locked", caller=caller)
            raise PwdNotUnlocked(
                f"portal-keys vault {PORTAL_KEYS_VAULT!r} is locked")
        self._touch(PORTAL_KEYS_VAULT)
        master_key = bytes(self._unlocked[PORTAL_KEYS_VAULT]["key"])
        tag = f"portal/{app_id}"
        try:
            existing = get_item_payload(VAULT_DIR, PORTAL_KEYS_VAULT,
                                        master_key, tag)
            self._audit.record("portal-get", PORTAL_KEYS_VAULT,
                               item_tag=tag, decision="allow",
                               reason="existing-key", caller=caller)
            return [dbus.Byte(b) for b in existing]
        except VaultNotFound:
            pass  # fall through to auto-provision
        except VaultIntegrityError as e:
            self._audit.record("portal-get", PORTAL_KEYS_VAULT,
                               item_tag=tag, decision="deny",
                               reason="integrity-fail", caller=caller)
            raise PwdIntegrityError(str(e))
        # Auto-provision a fresh per-app key. Tag is admin-readable so
        # admin can inspect / revoke individual app keys.
        new_key = os.urandom(PORTAL_KEY_BYTES)
        try:
            add_item(VAULT_DIR, PORTAL_KEYS_VAULT, master_key,
                     tag, new_key,
                     pin_uid=int(uid), replace=False)
        except (VaultDuplicate, VaultIntegrityError, ValueError) as e:
            # Race or corruption — re-read on duplicate so concurrent
            # provisioning of the same app_id converges.
            try:
                existing = get_item_payload(VAULT_DIR, PORTAL_KEYS_VAULT,
                                            master_key, tag)
                self._audit.record("portal-get", PORTAL_KEYS_VAULT,
                                   item_tag=tag, decision="allow",
                                   reason="race-recovered", caller=caller)
                return [dbus.Byte(b) for b in existing]
            except Exception:
                self._audit.record("portal-get", PORTAL_KEYS_VAULT,
                                   item_tag=tag, decision="deny",
                                   reason=f"provision-fail:{e}",
                                   caller=caller)
                raise PwdPolicyError(f"portal-key provision failed: {e}")
        self._audit.record("portal-get", PORTAL_KEYS_VAULT,
                           item_tag=tag, decision="allow",
                           reason="provisioned", caller=caller)
        return [dbus.Byte(b) for b in new_key]

    @dbus.service.method(BUS_NAME, in_signature="i", out_signature="aa{sv}",
                         sender_keyword="sender")
    def ListAuditLog(self, limit: int, sender=None) -> list[dict]:
        self._require_admin(sender)
        rows = self._audit.tail(int(limit))
        return [self._wrap_audit_row(r) for r in rows]

    @staticmethod
    def _wrap_audit_row(r: dict) -> dict:
        return {
            "id":             dbus.Int64(r["id"]),
            "ts":             dbus.Int64(r["ts"]),
            "op":             dbus.String(r["op"] or ""),
            "vault":          dbus.String(r["vault"] or ""),
            "item_tag":       dbus.String(r["item_tag"] or ""),
            "decision":       dbus.String(r["decision"] or ""),
            "reason":         dbus.String(r["reason"] or ""),
            "caller_uid":     dbus.Int32(r["caller_uid"] if r["caller_uid"] is not None else -1),
            "caller_pid":     dbus.Int32(r["caller_pid"] if r["caller_pid"] is not None else -1),
            "caller_exe":     dbus.String(r["caller_exe"] or ""),
            "caller_sha":     dbus.String(r["caller_sha"] or ""),
            "caller_selinux": dbus.String(r["caller_selinux"] or ""),
            "caller_cgroup":  dbus.String(r["caller_cgroup"] or ""),
        }

    # -- signals -------------------------------------------------------

    @dbus.service.signal(BUS_NAME, signature="s")
    def VaultUnlocked(self, name: str):
        pass

    @dbus.service.signal(BUS_NAME, signature="ss")
    def VaultLocked(self, name: str, reason: str):
        pass


def _on_term(*_user_data):
    # ``GLib.unix_signal_add(priority, signum, handler, *user_data)`` invokes
    # the handler with ``*user_data`` — NOT with the Python ``signal``-module
    # ``(signum, frame)`` convention. The pre-fix signature
    # ``def _on_term(signum, frame)`` raised ``TypeError: _on_term() missing
    # 1 required positional argument: 'frame'`` on SIGTERM/SIGINT, leaving
    # the daemon hanging in ``stop-sigterm`` after ``systemctl restart``
    # (Round-7 fix-pass review). Accept ``*user_data`` and return ``False``
    # so GLib removes the source after the loop quits.
    print("[qdistro-pwd] caught signal, shutting down", flush=True)
    if _LOOP is not None:
        GLib.MainLoop.quit(_LOOP)
    return False


_LOOP: GLib.MainLoop | None = None


def main():
    global _LOOP
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    name = dbus.service.BusName(BUS_NAME, bus, do_not_queue=True)  # noqa: F841
    daemon = PwdDaemon(bus)  # noqa: F841
    print(f"[qdistro-pwd] listening on {BUS_NAME} {OBJ_PATH}", flush=True)
    _LOOP = GLib.MainLoop()
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, _on_term, None)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, _on_term, None)
    _LOOP.run()


if __name__ == "__main__":
    main()
