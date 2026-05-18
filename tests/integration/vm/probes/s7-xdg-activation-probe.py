#!/usr/bin/env python3
"""§6.7 xdg-activation-v1 probe.

Verifies the qdwin compositor:
  1. Advertises xdg_activation_v1 as a global.
  2. Issues a unique non-empty token in response to
     get_activation_token + set_app_id + commit.
  3. Silently ignores an activate() with an unknown token (no error,
     no disconnect).

Runs entirely as a wayland client — no toplevel creation, no
weston-terminal. The compositor's weston_log output (inspected
out-of-band by the probe's wrapper shell) covers the
"token issued" / "activate with unknown token" log lines.
"""
from __future__ import annotations

import os
import sys

# Add qdshell/protocol to sys.path so the pre-generated pywayland
# bindings resolve. fresh-vm-bootstrap installs qdshell to
# /home/admin/qdshell via probes; s2-stream-smoke-style paths.
PROTO_DIR = os.environ.get("QDSHELL_PROTO_DIR", "/home/admin/qdshell")
sys.path.insert(0, PROTO_DIR)

from pywayland.client import Display  # noqa: E402
from pywayland.protocol.wayland import WlCompositor  # noqa: E402
from protocol.xdg_activation_v1 import XdgActivationV1  # noqa: E402


def log(msg: str) -> None:
    print(f"[s7-xdg-activation] {msg}", flush=True)


def main() -> int:
    display = Display()
    display.connect()
    state = {"activation": None, "compositor": None,
             "token": None, "done": False}

    registry = display.get_registry()

    def on_global(_, name: int, interface: str, version: int) -> None:
        if interface == XdgActivationV1.name:
            state["activation"] = registry.bind(
                name, XdgActivationV1, min(version, 1))
            log(f"bound xdg_activation_v1 @{name} v{version}")
        elif interface == WlCompositor.name:
            state["compositor"] = registry.bind(
                name, WlCompositor, min(version, 4))

    registry.dispatcher["global"] = on_global
    display.roundtrip()

    activation = state["activation"]
    compositor = state["compositor"]
    if activation is None:
        log("FAIL: xdg_activation_v1 not advertised")
        return 2
    if compositor is None:
        log("FAIL: wl_compositor not advertised")
        return 2

    # --- happy path: issue token ---
    tok = activation.get_activation_token()
    tok.set_app_id("qdistro.probe")

    def on_done(_, token_str: str) -> None:
        state["token"] = token_str
        state["done"] = True
        log(f"done token={token_str!r}")

    tok.dispatcher["done"] = on_done
    tok.commit()
    display.flush()

    # Pump events until we get done. pywayland Display.dispatch
    # returns after any event burst; loop with a bounded retry count.
    for _ in range(20):
        if state["done"]:
            break
        display.dispatch(block=False)
        display.roundtrip()

    if not state["done"] or not state["token"]:
        log("FAIL: no done event after commit")
        return 3
    if len(state["token"]) < 8:
        log(f"FAIL: token too short ({state['token']!r})")
        return 3
    log(f"PASS: token issued ({len(state['token'])} chars)")

    # --- activate with a bogus token targeting our own roleless
    #     surface: compositor must silently ignore (no protocol error,
    #     no disconnect). Exercises the "unknown token" code path. ---
    throwaway = compositor.create_surface()
    activation.activate("bogus-token-does-not-exist", throwaway)
    display.flush()
    display.roundtrip()
    log("PASS: activate(bogus_token, surface) did not disconnect")

    # --- activate with our real token on the same roleless surface:
    #     compositor finds the token but no matching qdwin_toplevel
    #     (the surface has no desktop role) and logs accordingly. ---
    activation.activate(state["token"], throwaway)
    display.flush()
    display.roundtrip()
    log("PASS: activate(valid_token, roleless_surface) did not disconnect")

    # Clean-up. Destroy the unused token object too (the real token
    # was bound to a different resource which is gone; actually both
    # are owned by `tok` — destroy it).
    tok.destroy()
    activation.destroy()
    display.flush()
    display.roundtrip()
    log("PASS: §6.7 xdg-activation-v1 probe")
    display.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
