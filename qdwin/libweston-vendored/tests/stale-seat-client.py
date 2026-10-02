#!/usr/bin/env python3
"""Client half of run-inert-relptr-test.sh.

Binds the test module's seat, has weston release it (by opening a second
connection, the module's trigger), waits for the wl_registry.global_remove,
then makes requests on the now-inert objects.

  control         a stale wl_pointer but no further request — proves the
                  release alone does not take weston down
  pointer-before  wl_pointer taken while the seat was live; relative pointer
                  requested after, then explicitly destroyed
  pointer-after   wl_pointer taken from the stale wl_seat; relative pointer
                  requested after and kept
  tablet-before   zwp_tablet_seat_v2 taken while the seat was live
  tablet-after    zwp_tablet_seat_v2 requested for the stale wl_seat

With --hold the client stays connected (objects alive) after printing its
verdict, until the runner stops weston — the graceful-shutdown case.

Exit 0: the compositor answered roundtrips after the requests.
Exit 1: the connection died (the compositor crashed or killed us).
Exit 2: setup problem (missing global, seat never removed).
"""

import sys
import time

from pywayland.client import Display
from pywayland.protocol.relative_pointer_unstable_v1 import (
    ZwpRelativePointerManagerV1,
)
from pywayland.protocol.tablet_unstable_v2 import ZwpTabletManagerV2
from pywayland.protocol.wayland import WlSeat


def answered(display):
    # roundtrip() returns -1 (it does not raise) once the connection is gone.
    try:
        return display.roundtrip() >= 0 and display.roundtrip() >= 0
    except Exception as exc:
        print(f"roundtrip raised: {exc}")
        return False


def main():
    mode = sys.argv[1]
    hold = "--hold" in sys.argv[2:]
    display = Display()
    display.connect()
    registry = display.get_registry()
    globals_by_iface = {}
    removed = set()

    def on_global(_reg, name, iface, version):
        globals_by_iface.setdefault(iface, []).append(name)

    def on_global_remove(_reg, name):
        removed.add(name)

    registry.dispatcher["global"] = on_global
    registry.dispatcher["global_remove"] = on_global_remove
    display.roundtrip()

    seats = globals_by_iface.get("wl_seat", [])
    relptr = globals_by_iface.get("zwp_relative_pointer_manager_v1", [])
    tablet = globals_by_iface.get("zwp_tablet_manager_v2", [])
    if len(seats) != 1 or len(relptr) != 1 or len(tablet) != 1:
        print(f"setup: seats={seats} relptr={relptr} tablet={tablet}")
        display.disconnect()
        return 2
    seat_name = seats[0]
    seat = registry.bind(seat_name, WlSeat, 1)
    relptr_mgr = registry.bind(relptr[0], ZwpRelativePointerManagerV1, 1)
    tablet_mgr = registry.bind(tablet[0], ZwpTabletManagerV2, 1)
    display.roundtrip()

    # Objects are kept referenced until exit so pywayland never destroys
    # them behind the test's back.
    kept = []
    pointer = seat.get_pointer() if mode in ("control", "pointer-before") else None
    if mode == "tablet-before":
        kept.append(tablet_mgr.get_tablet_seat(seat))
    display.roundtrip()

    trigger = Display()
    trigger.connect()
    trigger.roundtrip()
    for _ in range(200):
        if seat_name in removed:
            break
        display.roundtrip()
    if seat_name not in removed:
        print("setup: seat global was never removed")
        display.disconnect()
        trigger.disconnect()
        return 2

    if mode == "pointer-after":
        pointer = seat.get_pointer()
    if mode in ("pointer-before", "pointer-after"):
        relative = relptr_mgr.get_relative_pointer(pointer)
        kept.append(relative)
        if mode == "pointer-before":
            if not answered(display):
                print(f"{mode}: connection lost before destroy")
                return 1
            relative.destroy()
            kept.remove(relative)
    elif mode == "tablet-after":
        kept.append(tablet_mgr.get_tablet_seat(seat))

    if not answered(display):
        print(f"{mode}: connection lost")
        return 1
    print(f"{mode}: compositor answered", flush=True)
    if hold:
        # The runner terminates weston now; our objects are still alive.
        time.sleep(60)
        return 0
    display.disconnect()
    trigger.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
