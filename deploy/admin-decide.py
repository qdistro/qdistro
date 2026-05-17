#!/usr/bin/env python3
"""Run as admin (admin uid 1000) to approve or deny a pending request by id.
Equivalent to clicking the Approve/Deny buttons in the admin app — both
ultimately call DecideRequest on the broker. Used in Phase 1 because
wlroots virtual-keyboard input injection isn't reliable on labwc yet.

Usage: admin-decide.py <id> <allow|deny> [once|session]
"""
import sys
import dbus

BUS = "org.qdistro.AdminBroker1"
PATH = "/org/qdistro/AdminBroker1"


def main():
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    rid = int(sys.argv[1])
    decision = sys.argv[2]
    scope = sys.argv[3] if len(sys.argv) > 3 else "once"
    bus = dbus.SystemBus()
    obj = bus.get_object(BUS, PATH)
    obj.DecideRequest(rid, decision, scope, dbus_interface=BUS)
    print(f"decided: id={rid} decision={decision} scope={scope}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
