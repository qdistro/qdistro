#!/usr/bin/env python3
"""Client half of run-inert-relptr-test.sh.

Binds the test module's seat, has weston release it (by opening a second
connection, the module's trigger), waits for the wl_registry.global_remove, then asks for a
zwp_relative_pointer_v1 on the now-inert wl_pointer.

  mode "pointer-before": wl_pointer obtained while the seat was live
  mode "pointer-after":  wl_pointer obtained from the stale wl_seat
  mode "control":        same release and stale wl_pointer, but no
                         get_relative_pointer — proves the release alone
                         does not take weston down

Exit 0: the compositor answered a roundtrip after the request.
Exit 1: the connection died (the compositor crashed or killed us).
Exit 2: setup problem (missing global, seat never removed).
"""

import sys

from pywayland.client import Display
from pywayland.protocol.relative_pointer_unstable_v1 import (
    ZwpRelativePointerManagerV1,
)
from pywayland.protocol.wayland import WlSeat


def main():
    mode = sys.argv[1]
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
    managers = globals_by_iface.get("zwp_relative_pointer_manager_v1", [])
    if len(seats) != 1 or len(managers) != 1:
        print(f"setup: seats={seats} relptr_managers={managers}")
        display.disconnect()
        return 2
    seat_name = seats[0]
    seat = registry.bind(seat_name, WlSeat, 1)
    manager = registry.bind(managers[0], ZwpRelativePointerManagerV1, 1)
    display.roundtrip()

    pointer = seat.get_pointer() if mode == "pointer-before" else None
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

    if pointer is None:
        pointer = seat.get_pointer()
    if mode != "control":
        manager.get_relative_pointer(pointer)
    # roundtrip() returns -1 (it does not raise) once the connection is gone.
    try:
        answered = display.roundtrip() >= 0 and display.roundtrip() >= 0
    except Exception as exc:
        print(f"{mode}: roundtrip raised: {exc}")
        answered = False
    if not answered:
        print(f"{mode}: connection lost")
        return 1
    print(f"{mode}: compositor answered")
    display.disconnect()
    trigger.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
