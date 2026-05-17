#!/usr/bin/env python3
"""qdistro-pwd-get — app-side CLI for reading vault items.

Run by an arbitrary user-uid app to fetch its own pinned secret. The
broker daemon does the kernel-attested identity check; this CLI is just
a thin D-Bus shim so shell scripts and apps without a dbus client can
consume secrets.

Usage:
    qdistro-pwd-get <vault> <tag>

Exits 0 with the value on stdout (no trailing newline) on success;
exits non-zero on policy denial / missing item / locked vault.
"""
from __future__ import annotations

import sys

import dbus

BUS_NAME = "com.qdistro.Pwd1"
OBJ_PATH = "/com/qdistro/Pwd1"


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: qdistro-pwd-get <vault> <tag>", file=sys.stderr)
        return 2
    vault, tag = sys.argv[1], sys.argv[2]
    try:
        bus = dbus.SystemBus()
        obj = bus.get_object(BUS_NAME, OBJ_PATH)
        ifc = dbus.Interface(obj, BUS_NAME)
        value = str(ifc.GetItem(vault, tag))
    except dbus.DBusException as e:
        print(f"qdistro-pwd error: {e.get_dbus_name()}: {e.get_dbus_message()}",
              file=sys.stderr)
        return 3
    sys.stdout.write(value)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
