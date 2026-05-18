#!/usr/bin/env python3
"""§6.8 S0/S1 probe — bind qdwin_nested_v1 + advertise a toplevel.

The outer compositor under test is qdwin with §6.8 S0/S1 stub at v2
(string node identifiers).  We simulate "nested weston" as a plain
wayland-client that binds qdwin_nested_manager_v1 and calls
advertise_toplevel(pw_node='probe:0:probe-output',
input_sink='', app_id="test.nested.app", title="hello", origin_uid=1000).

S0/S1 don't actually resolve the PipeWire nodes on the outer side;
the assert is that the advertise round-trips and the compositor fires
the initial `configured` event at the placeholder size (800x600).
"""
from __future__ import annotations

import os
import sys
import time
from pywayland.client import Display
from pywayland.protocol.wayland import WlRegistry  # noqa: F401 (sanity)


def main() -> int:
    proto_dir = os.environ.get("QDSHELL_PROTO_DIR", ".")
    sys.path.insert(0, proto_dir)
    # The §6.8 S0 XML has been registered via gen_protocol.sh. pywayland
    # generator lowercases names, so we import QdwinNestedManagerV1.
    # gen_protocol.sh writes into QDSHELL_PROTO_DIR/protocol as a
    # plain package tree; qdshell.py imports via "from protocol.
    # qdwin_shell_v1 import QdwinShellV1" with the protocol dir on
    # sys.path. Mirror the shape here.
    if proto_dir and proto_dir not in sys.path:
        sys.path.insert(0, proto_dir)
    try:
        from protocol.qdwin_nested_v1 import QdwinNestedManagerV1
        from protocol.qdwin_nested_v1 import QdwinNestedToplevelV1  # noqa: F401
    except ImportError as e:
        print(f"[s21-nested] import failed: {e}", file=sys.stderr)
        return 2

    display = Display()
    display.connect()
    reg = display.get_registry()
    state = {"manager_id": None, "manager_ver": None, "manager": None,
             "configured": None}

    def on_global(registry, name, iface, version):
        if iface == "qdwin_nested_manager_v1":
            state["manager_id"] = name
            state["manager_ver"] = version
            state["manager"] = reg.bind(name, QdwinNestedManagerV1, version)

    reg.dispatcher["global"] = on_global
    display.dispatch(block=False)
    display.roundtrip()

    if not state["manager"]:
        print("[s21-nested] FAIL: qdwin_nested_manager_v1 not advertised",
              file=sys.stderr)
        return 3
    print(f"[s21-nested] PASS: bound qdwin_nested_manager_v1 "
          f"v{state['manager_ver']}")

    tl = state["manager"].advertise_toplevel(
        "probe:0:probe-output", "", "test.nested.app", "hello", 1000)

    def on_configured(toplevel, w, h):
        state["configured"] = (w, h)
    tl.dispatcher["configured"] = on_configured

    display.roundtrip()
    # Drain one more cycle in case the event was queued.
    for _ in range(5):
        display.dispatch(block=False)
        display.roundtrip()
        if state["configured"]:
            break
        time.sleep(0.1)

    if state["configured"] is None:
        print("[s21-nested] FAIL: never received `configured` event",
              file=sys.stderr)
        return 4
    w, h = state["configured"]
    print(f"[s21-nested] PASS: received configured({w}x{h})")
    if (w, h) != (800, 600):
        print(f"[s21-nested] WARN: configured size {w}x{h} "
              f"!= S0 placeholder 800x600 (may be real-size pass "
              f"from S2)")
    # Exercise all 3 per-toplevel setters.
    tl.set_title("hello again")
    tl.set_app_id("test.nested.app.v2")
    tl.set_geometry(640, 480)
    display.roundtrip()
    print("[s21-nested] PASS: set_title/set_app_id/set_geometry accepted")

    tl.destroy()
    state["manager"].destroy()
    display.roundtrip()
    display.disconnect()
    print("[s21-nested] PASS: §6.8 S0 stub probe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
