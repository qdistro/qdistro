#!/usr/bin/env python3
"""qdistro-pwd-admin — admin CLI for managing vaults + items.

Talks to com.qdistro.Pwd1 on the system bus. Must be run as the admin
uid (1000 / admin); the daemon enforces this on every admin-only call.

Usage:
    qdistro-pwd-admin list-vaults
    qdistro-pwd-admin create     <vault>            # v1 / scrypt password
    qdistro-pwd-admin create-tpm <vault>            # v2 / TPM-sealed + PIN
    qdistro-pwd-admin info       <vault>            # version + tpm backend
    qdistro-pwd-admin unlock     <vault>            # secret = pwd or PIN per version
    qdistro-pwd-admin rotate-vault <vault>          # change pwd (v1) or PIN (v2)
    qdistro-pwd-admin lock       <vault>
    qdistro-pwd-admin status     <vault>
    qdistro-pwd-admin add        <vault> <tag> [--pin-exe PATH] [--pin-selinux LABEL] [--pin-uid N]
    qdistro-pwd-admin get        <vault> <tag>      # admin bypass
    qdistro-pwd-admin delete     <vault> <tag>
    qdistro-pwd-admin items      <vault>
    qdistro-pwd-admin audit      [--limit N]
    qdistro-pwd-admin store-portal-pin                  # TPM-seal the portal-keys PIN
    qdistro-pwd-admin auto-unlock-portal-keys           # one-shot login auto-unlock
    qdistro-pwd-admin portal-pin-info                   # show stash metadata

`add` and `create` prompt for value/password on stdin. Use `--pin-uid -1`
or omit to leave uid unpinned.

The portal-keys auto-unlock flow: admin runs `store-portal-pin` once
(prompts for the PIN; daemon TPM-seals + writes to
`/var/lib/qdistro/portal-keys-pin.tpm`). A session systemd unit then
runs `auto-unlock-portal-keys` at login, so unmodified Flatpak apps
get their per-app portal Secret keys without a manual unlock.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from typing import Any

import dbus

BUS_NAME = "com.qdistro.Pwd1"
OBJ_PATH = "/com/qdistro/Pwd1"


def proxy() -> dbus.Interface:
    bus = dbus.SystemBus()
    obj = bus.get_object(BUS_NAME, OBJ_PATH)
    return dbus.Interface(obj, BUS_NAME)


def cmd_list_vaults(_args, ifc: dbus.Interface) -> int:
    for v in ifc.ListVaults():
        print(v)
    return 0


def cmd_create(args, ifc: dbus.Interface) -> int:
    pw1 = getpass.getpass(f"new password for vault {args.vault!r}: ")
    pw2 = getpass.getpass("confirm: ")
    if pw1 != pw2:
        print("passwords do not match", file=sys.stderr)
        return 2
    if not pw1:
        print("password must be non-empty", file=sys.stderr)
        return 2
    ok = ifc.CreateVault(args.vault, pw1)
    return 0 if ok else 3


def cmd_create_tpm(args, ifc: dbus.Interface) -> int:
    pin1 = getpass.getpass(f"new PIN for TPM-sealed vault {args.vault!r}: ")
    pin2 = getpass.getpass("confirm: ")
    if pin1 != pin2:
        print("PINs do not match", file=sys.stderr)
        return 2
    if not pin1:
        print("PIN must be non-empty", file=sys.stderr)
        return 2
    ok = ifc.CreateVaultTPM(args.vault, pin1)
    return 0 if ok else 3


def cmd_info(args, ifc: dbus.Interface) -> int:
    info = ifc.VaultInfo(args.vault)
    v = int(info.get("version", 0))
    backend = str(info.get("tpm_backend", ""))
    pcrs = str(info.get("tpm_pcrs", ""))
    if v == 1:
        print(f"vault={args.vault} version=1 kind=scrypt-password")
    elif v == 2:
        suffix = f" pcrs={pcrs}" if pcrs else " pcrs=(unbound)"
        print(f"vault={args.vault} version=2 kind=tpm-sealed "
              f"backend={backend}{suffix}")
    else:
        print(f"vault={args.vault} version={v} kind=unknown")
    return 0


def cmd_unlock(args, ifc: dbus.Interface) -> int:
    secret = os.environ.get("QDISTRO_PWD_PASSWORD")
    if secret is None:
        # Label the prompt by version so admin can see what kind of secret
        # is expected. Best-effort — falls back to "secret" if the lookup
        # fails (e.g. vault missing).
        try:
            v = int(ifc.VaultVersion(args.vault))
        except dbus.DBusException:
            v = 0
        kind = {1: "password", 2: "PIN"}.get(v, "secret")
        secret = getpass.getpass(f"{kind} for vault {args.vault!r}: ")
    ok = ifc.UnlockVault(args.vault, secret)
    print("unlocked" if ok else "denied")
    return 0 if ok else 3


def cmd_unlock_fprint(args, ifc: dbus.Interface) -> int:
    """Direct fprintd unlock — ask for the PIN, then call
    UnlockVaultFprint which gates on a fprintd Verify cycle.
    Bypasses polkit; admin-uid only on the daemon side."""
    secret = os.environ.get("QDISTRO_PWD_PASSWORD")
    if secret is None:
        try:
            v = int(ifc.VaultVersion(args.vault))
        except dbus.DBusException:
            v = 0
        kind = {1: "password", 2: "PIN"}.get(v, "secret")
        secret = getpass.getpass(f"{kind} for vault {args.vault!r}: ")
    print(f"[unlock-fprint] touch the fingerprint sensor for {args.vault!r}…",
          file=sys.stderr, flush=True)
    ok = ifc.UnlockVaultFprint(args.vault, secret)
    print("unlocked" if ok else "denied")
    return 0 if ok else 3


def cmd_rotate_vault(args, ifc: dbus.Interface) -> int:
    """Rotate a vault's secret without re-encrypting items.

    For v1 vaults: prompts for the old password and a new password.
    For v2 vaults: prompts for the old PIN and a new PIN. The vault's
    items are unchanged (master key is re-sealed, not regenerated).
    """
    try:
        v = int(ifc.VaultVersion(args.vault))
    except dbus.DBusException as e:
        print(f"qdistro-pwd error: {e.get_dbus_name()}: {e.get_dbus_message()}",
              file=sys.stderr)
        return 4
    kind = {1: "password", 2: "PIN"}.get(v, "secret")
    old_secret = os.environ.get("QDISTRO_PWD_OLD")
    if old_secret is None:
        old_secret = getpass.getpass(f"current {kind} for vault {args.vault!r}: ")
    new_secret = os.environ.get("QDISTRO_PWD_NEW")
    if new_secret is None:
        new1 = getpass.getpass(f"new {kind}: ")
        new2 = getpass.getpass("confirm: ")
        if new1 != new2:
            print(f"new {kind}s do not match", file=sys.stderr)
            return 2
        if not new1:
            print(f"new {kind} must be non-empty", file=sys.stderr)
            return 2
        new_secret = new1
    ok = ifc.RotateVault(args.vault, old_secret, new_secret)
    print("rotated" if ok else "denied")
    return 0 if ok else 3


def cmd_lock(args, ifc: dbus.Interface) -> int:
    ok = ifc.LockVault(args.vault)
    print("locked" if ok else "was not unlocked")
    return 0 if ok else 1


def cmd_status(args, ifc: dbus.Interface) -> int:
    print("unlocked" if bool(ifc.IsUnlocked(args.vault)) else "locked")
    return 0


def cmd_add(args, ifc: dbus.Interface) -> int:
    value = os.environ.get("QDISTRO_PWD_VALUE")
    if value is None:
        if sys.stdin.isatty():
            value = getpass.getpass(f"value for {args.tag!r}: ")
        else:
            value = sys.stdin.read().rstrip("\n")
    if not value:
        print("value must be non-empty", file=sys.stderr)
        return 2
    pin_uid = int(args.pin_uid) if args.pin_uid is not None else -1
    ok = ifc.AddItem(args.vault, args.tag, value,
                     args.pin_exe or "", args.pin_selinux or "",
                     pin_uid, "")
    return 0 if ok else 3


def cmd_get(args, ifc: dbus.Interface) -> int:
    s = ifc.GetItemAdmin(args.vault, args.tag)
    print(str(s))
    return 0


def cmd_delete(args, ifc: dbus.Interface) -> int:
    ok = ifc.DeleteItem(args.vault, args.tag)
    print("deleted" if ok else "absent")
    return 0 if ok else 1


def cmd_items(args, ifc: dbus.Interface) -> int:
    items = ifc.ListItems(args.vault)
    if not items:
        print("(empty)")
        return 0
    print(f"{'TAG':<28} {'PIN-EXE':<32} {'PIN-SELINUX':<24} {'PIN-UID':>7}")
    for it in items:
        print(f"{str(it['tag']):<28} "
              f"{str(it.get('pin_app_exe','')):<32} "
              f"{str(it.get('pin_selinux','')):<24} "
              f"{int(it.get('pin_uid', -1)):>7}")
    return 0


def cmd_store_portal_pin(args, ifc: dbus.Interface) -> int:
    pin = os.environ.get("QDISTRO_PWD_PORTAL_PIN")
    if pin is None:
        pin1 = getpass.getpass("portal-keys PIN: ")
        pin2 = getpass.getpass("confirm: ")
        if pin1 != pin2:
            print("PINs do not match", file=sys.stderr)
            return 2
        pin = pin1
    if not pin:
        print("PIN must be non-empty", file=sys.stderr)
        return 2
    ay = [dbus.Byte(b) for b in pin.encode("utf-8")]
    ok = ifc.StashPortalPin(ay)
    print("portal-keys PIN sealed" if ok else "stash failed")
    return 0 if ok else 3


def cmd_auto_unlock_portal_keys(_args, ifc: dbus.Interface) -> int:
    ok = ifc.AutoUnlockPortalKeys()
    print("portal-keys unlocked" if ok else "denied")
    return 0 if ok else 3


def cmd_portal_pin_info(_args, ifc: dbus.Interface) -> int:
    info = ifc.PortalPinStashInfo()
    present = bool(info.get("present", False))
    if not present:
        print("portal-keys PIN stash absent — run `store-portal-pin`")
        return 1
    print(f"portal-keys PIN stash: backend={str(info['backend'])} "
          f"created_at_unix={int(info['created_at_unix'])} "
          f"path={str(info['stash_path'])}")
    return 0


def cmd_audit(args, ifc: dbus.Interface) -> int:
    rows = ifc.ListAuditLog(int(args.limit))
    if not rows:
        print("(no audit entries)")
        return 0
    for r in rows:
        ts = int(r["ts"])
        print(f"#{int(r['id']):>5} ts={ts} op={str(r['op']):<10} "
              f"vault={str(r['vault']):<14} "
              f"tag={str(r['item_tag']):<20} "
              f"decision={str(r['decision']):<6} "
              f"caller_uid={int(r['caller_uid']):>5} "
              f"reason={str(r['reason'])}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="qdistro-pwd-admin")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list-vaults").set_defaults(fn=cmd_list_vaults)
    sp = sub.add_parser("create");     sp.add_argument("vault"); sp.set_defaults(fn=cmd_create)
    sp = sub.add_parser("create-tpm"); sp.add_argument("vault"); sp.set_defaults(fn=cmd_create_tpm)
    sp = sub.add_parser("info");       sp.add_argument("vault"); sp.set_defaults(fn=cmd_info)
    sp = sub.add_parser("unlock"); sp.add_argument("vault"); sp.set_defaults(fn=cmd_unlock)
    sp = sub.add_parser("unlock-fprint"); sp.add_argument("vault")
    sp.set_defaults(fn=cmd_unlock_fprint)
    sp = sub.add_parser("rotate-vault"); sp.add_argument("vault")
    sp.set_defaults(fn=cmd_rotate_vault)
    sp = sub.add_parser("lock");   sp.add_argument("vault"); sp.set_defaults(fn=cmd_lock)
    sp = sub.add_parser("status"); sp.add_argument("vault"); sp.set_defaults(fn=cmd_status)
    sp = sub.add_parser("add")
    sp.add_argument("vault")
    sp.add_argument("tag")
    sp.add_argument("--pin-exe", default="")
    sp.add_argument("--pin-selinux", default="")
    sp.add_argument("--pin-uid", type=int, default=None)
    sp.set_defaults(fn=cmd_add)
    sp = sub.add_parser("get");    sp.add_argument("vault"); sp.add_argument("tag"); sp.set_defaults(fn=cmd_get)
    sp = sub.add_parser("delete"); sp.add_argument("vault"); sp.add_argument("tag"); sp.set_defaults(fn=cmd_delete)
    sp = sub.add_parser("items");  sp.add_argument("vault"); sp.set_defaults(fn=cmd_items)
    sp = sub.add_parser("audit");  sp.add_argument("--limit", type=int, default=20); sp.set_defaults(fn=cmd_audit)
    sub.add_parser("store-portal-pin").set_defaults(fn=cmd_store_portal_pin)
    sub.add_parser("auto-unlock-portal-keys").set_defaults(fn=cmd_auto_unlock_portal_keys)
    sub.add_parser("portal-pin-info").set_defaults(fn=cmd_portal_pin_info)
    args = p.parse_args(argv)
    try:
        return args.fn(args, proxy())
    except dbus.DBusException as e:
        print(f"qdistro-pwd error: {e.get_dbus_name()}: {e.get_dbus_message()}",
              file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
