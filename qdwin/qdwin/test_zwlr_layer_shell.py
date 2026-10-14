#!/usr/bin/env python3
"""Tier-1 protocol unit tests for zwlr_layer_shell_v1 in qdwin.

Each test connects a fresh pywayland client to a running weston with
qdwin-shell.so loaded, drives one protocol path, and asserts the
expected outcome.

Detection model: pywayland 0.4.17 does not surface wl_display.error
events through user dispatchers — libwayland prints them to stderr
and the connection becomes unusable but no exception is raised. We
redirect stderr to a temp file and grep it for the libwayland error
format (`<interface>#<id>: error <code>: <msg>`).

Usage:
    python3 test_zwlr_layer_shell.py <wayland-display>

Exit code: 0 if all pass, 1 if any fails.

Phase 1.6 of .
"""
import os
import re
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "qdshell"))

from pywayland.client import Display  # noqa: E402
from pywayland.protocol.wayland import (  # noqa: E402
    WlCompositor, WlShm,
)
from protocol.wlr_layer_shell_unstable_v1 import (  # noqa: E402
    ZwlrLayerShellV1, ZwlrLayerSurfaceV1,
)
from protocol.xdg_shell import XdgWmBase, XdgPositioner  # noqa: E402
try:  # qdwin_shell_v1 may be absent on installs that didn't gen this protocol
    from protocol.qdwin_shell_v1 import QdwinShellV1  # noqa: E402
except Exception:
    QdwinShellV1 = None  # type: ignore
try:  # weston_test: only when the harness loaded the test input module
    from protocol.weston_test import WestonTest  # noqa: E402
except Exception:
    WestonTest = None  # type: ignore


def _vendored_libweston_active():
    """True iff the compositor was launched against the qdistro-vendored
    libweston-16 (the NULL-parent xdg_popup / layer-popup-grab patch).

    The harness that boots weston (run-protocol-tests.sh in a VM, or
    tests/protocol/run-layer-shell-protocol-test.sh headless) exports
    QDWIN_USE_VENDORED_LIBWESTON=1 into BOTH the weston env and this
    client's env when it LD_LIBRARY_PATH-prefixes the vendored .so, so
    the two agree. The grab-handler and layer-popup-reposition tests are
    discriminators that are only meaningful against the patched
    libweston; against stock libweston-16 (which lacks
    weston_desktop_xdg_popup_set_layer_grab_handler) they SKIP rather
    than FAIL, so the protocol suite stays green on a stock host.
    """
    return os.environ.get("QDWIN_USE_VENDORED_LIBWESTON") == "1"


# Stderr capture: libwayland's protocol-error printout lands here.
_STDERR_LOG_FD = None
_STDERR_LOG_PATH = None
_ORIG_STDERR_FD = None


def _setup_stderr_capture():
    global _STDERR_LOG_FD, _STDERR_LOG_PATH, _ORIG_STDERR_FD
    f, _STDERR_LOG_PATH = tempfile.mkstemp(prefix="zwlr-test-stderr-")
    _STDERR_LOG_FD = f
    _ORIG_STDERR_FD = os.dup(2)
    os.dup2(f, 2)


def _restore_stderr():
    if _ORIG_STDERR_FD is not None:
        os.dup2(_ORIG_STDERR_FD, 2)


def _read_captured_stderr_since(offset):
    os.fsync(_STDERR_LOG_FD)
    with open(_STDERR_LOG_PATH, "rb") as f:
        f.seek(offset)
        return f.read().decode("utf-8", "replace")


def _stderr_size():
    try:
        return os.fstat(_STDERR_LOG_FD).st_size
    except OSError:
        return 0


# libwayland error printout pattern, e.g.:
#   "zwlr_layer_surface_v1#8: error 2: invalid anchor bitfield 0xff"
_WL_ERR_RE = re.compile(
    r"(\w+)#(\d+):\s+error\s+(\d+):\s+(.*)")


def with_globals(display_name):
    """Connect, find compositor + layer_shell + xdg_wm_base + shm."""
    state = {"compositor": None, "shell": None, "xdg": None, "shm": None}
    d = Display(display_name)
    d.connect()
    reg = d.get_registry()

    def on_global(_reg, name, interface, version):
        if interface == WlCompositor.name:
            state["compositor"] = _reg.bind(name, WlCompositor, min(version, 4))
        elif interface == ZwlrLayerShellV1.name:
            state["shell"] = _reg.bind(name, ZwlrLayerShellV1, min(version, 5))
        elif interface == XdgWmBase.name:
            state["xdg"] = _reg.bind(name, XdgWmBase, min(version, 3))
        elif interface == WlShm.name:
            state["shm"] = _reg.bind(name, WlShm, min(version, 1))

    reg.dispatcher["global"] = on_global
    reg.dispatcher["global_remove"] = lambda r, n: None
    d.roundtrip()
    return d, state


def expect_protocol_error(d, label, expect_iface=None, expect_code=None):
    """Roundtrip and grep captured stderr for libwayland's error
    printout. Optionally constrain to a specific interface and/or code.
    Returns True on match."""
    start = _stderr_size()
    try:
        d.roundtrip()
        d.roundtrip()
    except Exception:
        pass  # connection-broken is also a positive signal
    captured = _read_captured_stderr_since(start)
    for m in _WL_ERR_RE.finditer(captured):
        iface, _oid, code, msg = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        if expect_iface and iface != expect_iface:
            continue
        if expect_code is not None and code != expect_code:
            continue
        print(f"  PASS [{label}] {iface} error {code}: {msg[:80]}")
        return True
    print(f"  FAIL [{label}] no matching error in stderr "
          f"(want iface={expect_iface} code={expect_code}). "
          f"Got: {captured.strip()[:200]}")
    return False


def test_handshake(display_name):
    """bind → get_layer_surface → commit (no buffer) → configure → ack
    → re-commit → no error."""
    d, st = with_globals(display_name)
    if not (st["compositor"] and st["shell"]):
        print("  SKIP [handshake] missing globals")
        d.disconnect()
        return False
    surf = st["compositor"].create_surface()
    ls = st["shell"].get_layer_surface(surf, None,
                                        ZwlrLayerShellV1.layer.top.value,
                                        "test-handshake")
    ls.set_size(0, 30)
    ls.set_anchor(
        ZwlrLayerSurfaceV1.anchor.top.value
        | ZwlrLayerSurfaceV1.anchor.left.value
        | ZwlrLayerSurfaceV1.anchor.right.value
    )

    got_configure = []
    def on_configure(_ls, serial, w, h):
        got_configure.append((serial, w, h))
        _ls.ack_configure(serial)
    ls.dispatcher["configure"] = on_configure

    surf.commit()
    d.roundtrip()
    d.roundtrip()
    if not got_configure:
        print("  FAIL [handshake] no configure received")
        d.disconnect()
        return False
    serial, w, h = got_configure[0]
    print(f"  PASS [handshake] configure(serial={serial}, {w}x{h})")
    d.disconnect()
    return True


def test_invalid_layer(display_name):
    """get_layer_surface(layer=99) → INVALID_LAYER (1)."""
    d, st = with_globals(display_name)
    surf = st["compositor"].create_surface()
    st["shell"].get_layer_surface(surf, None, 99, "test-invalid-layer")
    ok = expect_protocol_error(d, "invalid_layer",
                                expect_iface="zwlr_layer_shell_v1",
                                expect_code=1)
    try: d.disconnect()
    except: pass
    return ok


def test_invalid_anchor(display_name):
    """set_anchor(0xff) → bits outside top/bottom/left/right → INVALID_ANCHOR."""
    d, st = with_globals(display_name)
    surf = st["compositor"].create_surface()
    ls = st["shell"].get_layer_surface(surf, None,
                                        ZwlrLayerShellV1.layer.top.value,
                                        "test-invalid-anchor")
    ls.set_anchor(0xff)
    ok = expect_protocol_error(d, "invalid_anchor",
                                expect_iface="zwlr_layer_surface_v1",
                                expect_code=2)
    try: d.disconnect()
    except: pass
    return ok


def test_invalid_keyboard_interactivity(display_name):
    """set_keyboard_interactivity(99) → INVALID_KEYBOARD_INTERACTIVITY."""
    d, st = with_globals(display_name)
    surf = st["compositor"].create_surface()
    ls = st["shell"].get_layer_surface(surf, None,
                                        ZwlrLayerShellV1.layer.top.value,
                                        "test-invalid-kbd")
    ls.set_keyboard_interactivity(99)
    ok = expect_protocol_error(d, "invalid_keyboard_interactivity",
                                expect_iface="zwlr_layer_surface_v1",
                                expect_code=3)
    try: d.disconnect()
    except: pass
    return ok


def test_invalid_exclusive_edge(display_name):
    """set_exclusive_edge(0xff) → bits outside anchor enum → INVALID_EXCLUSIVE_EDGE."""
    d, st = with_globals(display_name)
    surf = st["compositor"].create_surface()
    ls = st["shell"].get_layer_surface(surf, None,
                                        ZwlrLayerShellV1.layer.top.value,
                                        "test-invalid-edge")
    # Only meaningful for v5; if shell is v<5 the request doesn't exist.
    if not hasattr(ls, "set_exclusive_edge"):
        print("  SKIP [invalid_exclusive_edge] manager <v5")
        try: d.disconnect()
        except: pass
        return True
    ls.set_exclusive_edge(0xff)
    ok = expect_protocol_error(d, "invalid_exclusive_edge",
                                expect_iface="zwlr_layer_surface_v1",
                                expect_code=4)
    try: d.disconnect()
    except: pass
    return ok


def test_invalid_layer_set_layer(display_name):
    """set_layer(99) on the layer_surface → INVALID_LAYER."""
    d, st = with_globals(display_name)
    surf = st["compositor"].create_surface()
    ls = st["shell"].get_layer_surface(surf, None,
                                        ZwlrLayerShellV1.layer.top.value,
                                        "test-set-layer-bad")
    if not hasattr(ls, "set_layer"):
        print("  SKIP [set_layer-bad] manager <v2")
        try: d.disconnect()
        except: pass
        return True
    ls.set_layer(99)
    ok = expect_protocol_error(d, "set_layer-bad",
                                expect_iface="zwlr_layer_surface_v1",
                                expect_code=1)
    try: d.disconnect()
    except: pass
    return ok


def test_null_parent_popup(display_name):
    """xdg_surface.get_popup(parent=NULL) gating for the vendored
    libweston-16 NULL-parent xdg_popup patch.

    Stock libweston-16 unconditionally posts xdg_wm_base#3
    (invalid_popup_parent) at construction time. The qdistro patch
    accepts NULL at construction and only enforces the spec error if
    commit happens with parent still NULL.

    Set QDWIN_USE_VENDORED_LIBWESTON=1 in the env that launched weston
    to flip the expected outcome. The test reads the same env (the
    test client and weston are launched by the same harness, so they
    agree). With the env unset the test asserts stock behaviour
    (error MUST fire); with it set the test asserts patched behaviour
    (error MUST NOT fire on construction).
    """
    expect_vendored = os.environ.get("QDWIN_USE_VENDORED_LIBWESTON") == "1"
    label = "null_parent_popup"

    d, st = with_globals(display_name)
    if not st["xdg"]:
        print(f"  SKIP [{label}] xdg_wm_base unavailable")
        try: d.disconnect()
        except: pass
        return True

    positioner = st["xdg"].create_positioner()
    positioner.set_size(50, 50)
    positioner.set_anchor_rect(0, 0, 1, 1)

    surf = st["compositor"].create_surface()
    xs = st["xdg"].get_xdg_surface(surf)
    # The whole point: parent=None at construction. Do NOT commit —
    # patched libweston defers the parent error to commit-time, and
    # we want construction-time behaviour as the discriminator.
    xs.get_popup(None, positioner)

    start = _stderr_size()
    try:
        d.roundtrip()
        d.roundtrip()
    except Exception:
        pass
    captured = _read_captured_stderr_since(start)

    # Stock libweston posts the error on the resource that called
    # get_popup (the xdg_surface), not the xdg_wm_base global. The
    # error code is XDG_WM_BASE_ERROR_INVALID_POPUP_PARENT (3) and
    # the message string carries "popup parent must be" — that text
    # is the discriminator we trust.
    saw_invalid_popup_parent = False
    for m in _WL_ERR_RE.finditer(captured):
        _iface, _oid, code, msg = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        if code == 3 and "popup parent" in msg:
            saw_invalid_popup_parent = True
            break

    try: d.disconnect()
    except: pass

    if expect_vendored:
        if saw_invalid_popup_parent:
            print(f"  FAIL [{label}] vendored libweston still posted "
                  f"invalid_popup_parent on get_popup(NULL): "
                  f"{captured.strip()[:200]}")
            return False
        print(f"  PASS [{label}] vendored libweston accepted get_popup(NULL)")
        return True
    else:
        if not saw_invalid_popup_parent:
            print(f"  FAIL [{label}] stock libweston should have posted "
                  f"invalid_popup_parent on get_popup(NULL) but did not. "
                  f"stderr: {captured.strip()[:200]}")
            return False
        print(f"  PASS [{label}] stock libweston rejected get_popup(NULL) as expected")
        return True


def test_v19_register_hotkey_live(display_name):
    """Live API contract for qdwin_shell_v1@v19 register_hotkey/unregister_hotkey.

    Binds the shell global, calls bind_as_shell + register_hotkey +
    unregister_hotkey, and asserts the compositor logged the
    expected lines from qdwin.c:qdwin_handle_{register,unregister}_hotkey.

    Real key-press → hotkey_pressed event delivery is intentionally
    NOT exercised here: headless weston has no input backend, see
    tests/host/AGENTS.md. This test proves the request
    surface is reachable, the binding allocates without error, and
    the teardown path runs — anything stronger waits on weston-test
    or ext-virtual-pointer-v1.

    Requires:
      - QDWIN_WESTON_LOG: path to the weston log of the compositor
        we're talking to. Skipped if unset (so the in-VM
        run-protocol-tests.sh suite, which doesn't pass it, keeps
        running).
      - QDWIN_ALLOWED_UID == os.getuid() in the compositor's env
        (otherwise bind is rejected).
    """
    label = "v19_register_hotkey_live"
    if QdwinShellV1 is None:
        print(f"  SKIP [{label}] qdwin_shell_v1 protocol module unavailable")
        return True
    log_path = os.environ.get("QDWIN_WESTON_LOG")
    if not log_path or not os.path.exists(log_path):
        print(f"  SKIP [{label}] QDWIN_WESTON_LOG unset / not found")
        return True

    d = Display(display_name)
    d.connect()
    reg = d.get_registry()
    state = {"shell": None, "ver": 0}

    def on_global(_reg, name, interface, version):
        if interface == QdwinShellV1.name:
            v = min(version, 19)
            state["shell"] = _reg.bind(name, QdwinShellV1, v)
            state["ver"] = v

    reg.dispatcher["global"] = on_global
    reg.dispatcher["global_remove"] = lambda r, n: None
    d.roundtrip()

    if state["shell"] is None:
        print(f"  SKIP [{label}] qdwin_shell_v1 not advertised "
              "(QDWIN_ALLOWED_UID mismatch?)")
        try: d.disconnect()
        except: pass
        return True
    if state["ver"] < 19:
        print(f"  SKIP [{label}] qdwin_shell_v1 advertised at v{state['ver']} (<19)")
        try: d.disconnect()
        except: pass
        return True

    shell = state["shell"]

    log_size_before = os.stat(log_path).st_size

    try:
        shell.bind_as_shell()
        # Pick KEY_F13 (linux input keycode 183) + Super|Shift mods.
        # F13 is unlikely to clash with weston's built-in admin
        # bindings on KEY_SPACE/KEY_TAB/etc.
        shell.register_hotkey(42, 4 | 8, 183)
        d.roundtrip()
        # Idempotent replace under same id.
        shell.register_hotkey(42, 4, 183)
        d.roundtrip()
        shell.unregister_hotkey(42)
        d.roundtrip()
    finally:
        try: d.disconnect()
        except: pass

    # Allow the compositor's logger a beat to flush.
    import time as _time
    for _ in range(20):
        with open(log_path, "rb") as f:
            f.seek(log_size_before)
            tail = f.read().decode("utf-8", "replace")
        if "qdwin: register_hotkey id=42" in tail and \
           "qdwin: unregister_hotkey id=42" in tail:
            break
        _time.sleep(0.05)

    needles = [
        "qdwin: register_hotkey id=42 mods=0xc key=183",
        "qdwin: register_hotkey id=42 mods=0x4 key=183",  # idempotent replace
        "qdwin: unregister_hotkey id=42",
    ]
    missing = [n for n in needles if n not in tail]
    if missing:
        print(f"  FAIL [{label}] missing log lines: {missing!r}\n"
              f"  log tail (last 400b):\n{tail[-400:]}")
        return False
    print(f"  PASS [{label}] register/unregister/replace all logged "
          f"on qdwin_shell_v1@v{state['ver']}")
    return True


def test_role_conflict(display_name):
    """get_layer_surface on a wl_surface that already has an xdg_toplevel
    role → ROLE error from weston_surface_set_role."""
    d, st = with_globals(display_name)
    if not st["xdg"]:
        print("  SKIP [role-conflict] xdg_wm_base unavailable")
        try: d.disconnect()
        except: pass
        return True
    surf = st["compositor"].create_surface()
    xs = st["xdg"].get_xdg_surface(surf)
    xs.get_toplevel()  # assigns xdg_toplevel role
    surf.commit()
    d.roundtrip()
    # Now try to assign zwlr_layer_surface_v1 role to the same surface.
    st["shell"].get_layer_surface(surf, None,
                                   ZwlrLayerShellV1.layer.top.value,
                                   "test-role-conflict")
    ok = expect_protocol_error(d, "role-conflict",
                                expect_iface="zwlr_layer_shell_v1",
                                expect_code=0)
    try: d.disconnect()
    except: pass
    return ok


def test_layer_popup_grab_stale_serial(display_name):
    """plan3 H1: xdg_popup.grab on a layer-parented popup with a stale
    serial must post XDG_POPUP_ERROR_INVALID_GRAB.

    Stock libweston posts INVALID_GRAB unconditionally for any
    NULL-parent popup. Patched libweston with qdwin's layer-grab
    handler validates the serial; a never-seen serial of 0 cannot
    match any pointer/keyboard/touch grab_serial, so the handler
    refuses and libweston posts the standard INVALID_GRAB. Either
    way the discriminator is: serial=0 grab attempt is rejected.
    """
    label = "layer_popup_grab_stale_serial"
    d, st = with_globals(display_name)
    if not (st["xdg"] and st["shell"] and st["compositor"]):
        print(f"  SKIP [{label}] missing globals")
        try: d.disconnect()
        except: pass
        return True

    # Layer-shell surface that will own the popup.
    parent_surf = st["compositor"].create_surface()
    ls = st["shell"].get_layer_surface(
        parent_surf, None,
        ZwlrLayerShellV1.layer.top.value, "test-grab-parent")
    ls.set_size(80, 80)
    ls.set_anchor(ZwlrLayerSurfaceV1.anchor.top.value
                  | ZwlrLayerSurfaceV1.anchor.left.value)
    ls.dispatcher["configure"] = lambda _ls, s, w, h: _ls.ack_configure(s)
    parent_surf.commit()
    d.roundtrip()

    # xdg_popup with NULL parent; layer-shell get_popup attaches the
    # layer-surface as parent before commit (the qdistro patched path).
    positioner = st["xdg"].create_positioner()
    positioner.set_size(40, 40)
    positioner.set_anchor_rect(0, 0, 1, 1)
    popup_surf = st["compositor"].create_surface()
    popup_xs = st["xdg"].get_xdg_surface(popup_surf)
    popup = popup_xs.get_popup(None, positioner)
    if not hasattr(ls, "get_popup"):
        print(f"  SKIP [{label}] zwlr_layer_surface_v1 < v3 (no get_popup)")
        try: d.disconnect()
        except: pass
        return True
    ls.get_popup(popup)

    # Grab with serial=0 — guaranteed stale, no input grab matches.
    # We need a seat resource for popup.grab.
    seat = None
    reg = d.get_registry()
    def on_g(_r, name, iface, ver):
        nonlocal seat
        if iface == "wl_seat":
            from pywayland.protocol.wayland import WlSeat
            seat = _r.bind(name, WlSeat, min(ver, 4))
    reg.dispatcher["global"] = on_g
    reg.dispatcher["global_remove"] = lambda r, n: None
    d.roundtrip()
    if not seat:
        print(f"  SKIP [{label}] no wl_seat global")
        try: d.disconnect()
        except: pass
        return True

    start = _stderr_size()
    popup.grab(seat, 0)
    try:
        d.roundtrip(); d.roundtrip()
    except Exception:
        pass
    captured = _read_captured_stderr_since(start)
    saw_invalid_grab = False
    for m in _WL_ERR_RE.finditer(captured):
        iface, _oid, code, _msg = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        if iface == "xdg_popup" and code == 0:
            saw_invalid_grab = True
            break

    try: d.disconnect()
    except: pass

    if saw_invalid_grab:
        print(f"  PASS [{label}] stale-serial grab rejected with INVALID_GRAB")
        return True
    print(f"  FAIL [{label}] expected xdg_popup error 0 "
          f"(INVALID_GRAB) on serial=0; "
          f"got: {captured.strip()[:200]}")
    return False


def test_layer_popup_nested_grab_routed(display_name):
    """D9: a nested xdg_popup.grab whose parent chain ends at a layer popup
    is routed to qdwin's layer-grab handler, never to libweston's desktop
    seat grab (an owner-events grab that would hand the popup client's
    other surfaces input and orphan the root popup's grab).

    Discriminator: P1 never took a grab. The stock path posts no error
    (with serial=0 the seat grab start fails and the popup is simply
    dismissed); the routed path answers per xdg_popup.grab ("if the parent
    is a popup that did not take an explicit grab, an error will be
    raised") with xdg_wm_base.invalid_popup_parent (3). The D9 follow-up
    tests below (layer_chain_*) drive real grabs through the chain with the
    test input seat; the live lane
    qdwin/tests/gui/agent-layer-popup-nested-grab-smoke.sh does it on a VM.
    """
    label = "layer_popup_nested_grab_routed"
    if not _vendored_libweston_active():
        print(f"  SKIP [{label}] stock libweston-16 has no layer-chain "
              f"routing; set QDWIN_USE_VENDORED_LIBWESTON=1")
        return True
    d, st = with_globals(display_name)
    if not (st["xdg"] and st["shell"] and st["compositor"]):
        print(f"  SKIP [{label}] missing globals")
        try: d.disconnect()
        except: pass
        return True

    parent_surf = st["compositor"].create_surface()
    ls = st["shell"].get_layer_surface(
        parent_surf, None,
        ZwlrLayerShellV1.layer.top.value, "test-nested-grab-parent")
    ls.set_size(80, 80)
    ls.set_anchor(ZwlrLayerSurfaceV1.anchor.top.value
                  | ZwlrLayerSurfaceV1.anchor.left.value)
    ls.dispatcher["configure"] = lambda _ls, s, w, h: _ls.ack_configure(s)
    parent_surf.commit()
    d.roundtrip()
    if not hasattr(ls, "get_popup"):
        print(f"  SKIP [{label}] zwlr_layer_surface_v1 < v3 (no get_popup)")
        try: d.disconnect()
        except: pass
        return True

    def positioner():
        pos = st["xdg"].create_positioner()
        pos.set_size(40, 40)
        pos.set_anchor_rect(0, 0, 1, 1)
        return pos

    # P1: layer-parented root popup. P2: nested submenu on P1.
    p1_surf = st["compositor"].create_surface()
    p1_xs = st["xdg"].get_xdg_surface(p1_surf)
    p1 = p1_xs.get_popup(None, positioner())
    ls.get_popup(p1)
    p2_surf = st["compositor"].create_surface()
    p2_xs = st["xdg"].get_xdg_surface(p2_surf)
    p2 = p2_xs.get_popup(p1_xs, positioner())

    seat = None
    reg = d.get_registry()
    def on_g(_r, name, iface, ver):
        nonlocal seat
        if iface == "wl_seat":
            from pywayland.protocol.wayland import WlSeat
            seat = _r.bind(name, WlSeat, min(ver, 4))
    reg.dispatcher["global"] = on_g
    reg.dispatcher["global_remove"] = lambda r, n: None
    d.roundtrip()
    if not seat:
        print(f"  SKIP [{label}] no wl_seat global")
        try: d.disconnect()
        except: pass
        return True

    start = _stderr_size()
    p2.grab(seat, 0)
    try:
        d.roundtrip(); d.roundtrip()
    except Exception:
        pass
    captured = _read_captured_stderr_since(start)
    routed = False
    for m in _WL_ERR_RE.finditer(captured):
        iface, code, msg = m.group(1), int(m.group(3)), m.group(4)
        if (iface == "xdg_wm_base" and code == 3
                and "without an explicit grab" in msg):
            routed = True
            break
    del p1

    try: d.disconnect()
    except: pass

    if routed:
        print(f"  PASS [{label}] nested grab under an ungrabbed layer popup "
              f"refused invalid_popup_parent on the layer-chain route")
        return True
    print(f"  FAIL [{label}] expected xdg_wm_base error 3 "
          f"(invalid_popup_parent) for a grab under an ungrabbed layer "
          f"popup; got: {captured.strip()[:200]}")
    return False


def test_popup_double_construct_refused(display_name):
    """D9 (sol r3): a second get_popup on the same xdg_surface is refused
    with xdg_surface.already_constructed. The vendored libweston links each
    popup into its parent's child list; a re-construction would re-link it
    into a second list and corrupt both."""
    label = "popup_double_construct_refused"
    if not _vendored_libweston_active():
        print(f"  SKIP [{label}] NULL-parent popups need the vendored "
              f"libweston; set QDWIN_USE_VENDORED_LIBWESTON=1")
        return True
    d, st = with_globals(display_name)
    if not (st["xdg"] and st["compositor"]):
        print(f"  SKIP [{label}] missing globals")
        try: d.disconnect()
        except: pass
        return True
    def positioner():
        pos = st["xdg"].create_positioner()
        pos.set_size(40, 40)
        pos.set_anchor_rect(0, 0, 1, 1)
        return pos
    surf = st["compositor"].create_surface()
    xs = st["xdg"].get_xdg_surface(surf)
    xs.get_popup(None, positioner())
    start = _stderr_size()
    xs.get_popup(None, positioner())
    try:
        d.roundtrip(); d.roundtrip()
    except Exception:
        pass
    captured = _read_captured_stderr_since(start)
    refused = any(m.group(1) == "xdg_surface" and int(m.group(3)) == 2
                  for m in _WL_ERR_RE.finditer(captured))
    try: d.disconnect()
    except: pass
    if refused:
        print(f"  PASS [{label}] second get_popup refused already_constructed")
        return True
    print(f"  FAIL [{label}] expected xdg_surface error 2 "
          f"(already_constructed); got: {captured.strip()[:200]}")
    return False


# ---------------------------------------------------------------------------
# D9 follow-ups: live layer-chain grabs on the test input seat.
#
# The harness loads tests/protocol/weston-test-input-module.c, a seat named
# "qdwin-test-input" (pointer, keyboard, touch) driven over weston_test.
# These tests take REAL grabs on it — press/touch serials, outside presses —
# which the stale-serial tests above cannot. Layout (global coordinates,
# headless output at 0,0):
#
#   L1  layer surface (top layer)          (0,0)    50x50
#   P1  root popup on L1                   (60,0)   40x40
#   P2  submenu, parent P1                 (100,0)  40x40
#   L2  sibling layer surface, same client (300,0)  50x50
#   Q   ungrabbed root popup on L2         (300,60) 40x40
#   (600,300) is empty: an outside press lands on nothing.
# ---------------------------------------------------------------------------

BTN_LEFT = 0x110
TEST_SEAT = "qdwin-test-input"
XDG_POSITIONER_ANCHOR_TOP_LEFT = 5
XDG_POSITIONER_GRAVITY_BOTTOM_RIGHT = 8


class _D9Session:
    """One client connection with the globals, the test seat's pointer and
    touch, and an event log the tests assert on."""

    def __init__(self, display_name):
        from pywayland.protocol.wayland import WlSeat
        self.ok = False
        self.why = ""
        self.d = Display(display_name)
        self.d.connect()
        self.g = {}
        self.seats = []
        self.events = []          # (kind, name, *details)
        self.serial = None        # latest press / touch-down serial
        self.names = {}           # surface ptr -> name
        reg = self.d.get_registry()

        def on_global(r, name, iface, ver):
            if iface == WlCompositor.name:
                self.g["compositor"] = r.bind(name, WlCompositor, min(ver, 4))
            elif iface == ZwlrLayerShellV1.name:
                self.g["shell"] = r.bind(name, ZwlrLayerShellV1, min(ver, 5))
            elif iface == XdgWmBase.name:
                self.g["xdg"] = r.bind(name, XdgWmBase, min(ver, 3))
            elif iface == WlShm.name:
                self.g["shm"] = r.bind(name, WlShm, 1)
            elif iface == "weston_test" and WestonTest is not None:
                self.g["test"] = r.bind(name, WestonTest, 1)
            elif iface == "wl_seat":
                s = r.bind(name, WlSeat, min(ver, 5))
                rec = {"seat": s, "name": None, "caps": 0}
                s.dispatcher["name"] = lambda _s, n, rec=rec: rec.update(name=n)
                s.dispatcher["capabilities"] = (
                    lambda _s, c, rec=rec: rec.update(caps=c))
                self.seats.append(rec)

        reg.dispatcher["global"] = on_global
        reg.dispatcher["global_remove"] = lambda r, n: None
        self.d.roundtrip()
        self.d.roundtrip()
        missing = [k for k in ("compositor", "shell", "xdg", "shm", "test")
                   if k not in self.g]
        if missing:
            self.why = "missing globals: " + ", ".join(missing)
            return
        self.g["xdg"].dispatcher["ping"] = lambda x, serial: x.pong(serial)
        rec = next((r for r in self.seats if r["name"] == TEST_SEAT), None)
        if rec is None:
            self.why = f"no wl_seat named {TEST_SEAT}"
            return
        self.seat = rec["seat"]
        self.pointer = self.seat.get_pointer()
        self.touch = self.seat.get_touch() if rec["caps"] & 4 else None
        self.pointer.dispatcher["enter"] = self._on_enter
        self.pointer.dispatcher["leave"] = lambda *a: None
        self.pointer.dispatcher["motion"] = lambda *a: None
        self.pointer.dispatcher["frame"] = lambda *a: None
        self.pointer.dispatcher["button"] = self._on_button
        if self.touch is not None:
            self.touch.dispatcher["down"] = self._on_touch_down
            self.touch.dispatcher["up"] = lambda *a: None
            self.touch.dispatcher["motion"] = lambda *a: None
            self.touch.dispatcher["frame"] = lambda *a: None
            self.touch.dispatcher["cancel"] = lambda *a: None
        self.ok = True

    # -- event log -----------------------------------------------------
    def _name(self, surface):
        if surface is None:
            return None
        for ptr, name in self.names.items():
            if getattr(surface, "_ptr", None) == ptr:
                return name
        return "?"

    def _on_enter(self, _p, serial, surface, sx, sy):
        self.events.append(("enter", self._name(surface)))

    def _on_button(self, _p, serial, time, button, state):
        if state == 1:
            self.serial = serial
        self.events.append(("button", self.focus_name(), state))

    def focus_name(self):
        for kind, *rest in reversed(self.events):
            if kind == "enter":
                return rest[0]
        return None

    def _on_touch_down(self, _t, serial, time, surface, tid, x, y):
        self.serial = serial
        self.events.append(("touch_down", self._name(surface)))

    def mark(self):
        return len(self.events)

    def since(self, mark, kind=None):
        return [e for e in self.events[mark:] if kind is None or e[0] == kind]

    # -- input on the test seat -----------------------------------------
    def _ts(self):
        import time
        ns = time.monotonic_ns()
        sec, nsec = divmod(ns, 1_000_000_000)
        return (sec >> 32, sec & 0xffffffff, nsec)

    def move(self, x, y):
        self.g["test"].move_pointer(*self._ts(), x, y)
        self.d.roundtrip()

    def click(self, x, y):
        self.move(x, y)
        self.g["test"].send_button(*self._ts(), BTN_LEFT, 1)
        self.d.roundtrip()
        self.g["test"].send_button(*self._ts(), BTN_LEFT, 0)
        self.d.roundtrip()

    def tap(self, x, y, tid=0):
        self.g["test"].send_touch(*self._ts(), tid, x, y, 0)   # down
        self.d.roundtrip()
        self.g["test"].send_touch(*self._ts(), tid, 0, 0, 1)   # up
        self.d.roundtrip()

    # -- surfaces --------------------------------------------------------
    def _buffer(self, w, h):
        stride = w * 4
        size = stride * h
        fd = os.memfd_create("d9-buf")
        os.ftruncate(fd, size)
        os.write(fd, b"\xff" * size)
        pool = self.g["shm"].create_pool(fd, size)
        buf = pool.create_buffer(0, w, h, stride, 0)   # argb8888
        pool.destroy()
        os.close(fd)
        return buf

    def _commit_and_repaint(self, surf):
        """Commit and wait for the frame callback. Input picking walks the
        compositor's view list, which is rebuilt at repaint: until the
        output has repainted, a freshly mapped surface takes no input."""
        import time
        done = []
        cb = surf.frame()
        cb.dispatcher["done"] = lambda _cb, t: done.append(t)
        surf.commit()
        deadline = time.monotonic() + 3.0
        while not done and time.monotonic() < deadline:
            try:
                self.d.roundtrip()
            except Exception:
                return
            time.sleep(0.005)

    def layer(self, name, w, h, margin_left=0):
        surf = self.g["compositor"].create_surface()
        self.names[surf._ptr] = name
        ls = self.g["shell"].get_layer_surface(
            surf, None, ZwlrLayerShellV1.layer.top.value, "d9-" + name)
        ls.set_size(w, h)
        ls.set_anchor(ZwlrLayerSurfaceV1.anchor.top.value
                      | ZwlrLayerSurfaceV1.anchor.left.value)
        if margin_left:
            ls.set_margin(0, 0, 0, margin_left)
        ls.dispatcher["configure"] = lambda _ls, s, cw, ch: _ls.ack_configure(s)
        surf.commit()
        self.d.roundtrip()
        surf.attach(self._buffer(w, h), 0, 0)
        self._commit_and_repaint(surf)
        return {"surf": surf, "ls": ls, "name": name}

    def popup(self, name, x, y, layer=None, parent=None, grab=None,
              wait=True):
        """A 40x40 popup at (x,y) relative to its parent: a root popup on a
        layer surface, or a submenu of another popup. grab=serial takes
        xdg_popup.grab before the first commit (the spec order). wait=False
        skips waiting for the repaint (a popup refused or dismissed at
        grab time never maps)."""
        pos = self.g["xdg"].create_positioner()
        pos.set_size(40, 40)
        pos.set_anchor_rect(x, y, 1, 1)
        pos.set_anchor(XDG_POSITIONER_ANCHOR_TOP_LEFT)
        pos.set_gravity(XDG_POSITIONER_GRAVITY_BOTTOM_RIGHT)
        surf = self.g["compositor"].create_surface()
        self.names[surf._ptr] = name
        xs = self.g["xdg"].get_xdg_surface(surf)
        p = xs.get_popup(parent["xs"] if parent else None, pos)
        rec = {"surf": surf, "xs": xs, "popup": p, "name": name,
               "configured": []}
        p.dispatcher["configure"] = lambda *a: None
        p.dispatcher["popup_done"] = (
            lambda _p, n=name: self.events.append(("popup_done", n)))
        xs.dispatcher["configure"] = (
            lambda _xs, s, rec=rec: (_xs.ack_configure(s),
                                     rec["configured"].append(s)))
        if layer is not None:
            layer["ls"].get_popup(p)
        if grab is not None:
            p.grab(self.seat, grab)
        surf.commit()
        self.d.roundtrip()
        if rec["configured"]:
            surf.attach(self._buffer(40, 40), 0, 0)
            if wait:
                self._commit_and_repaint(surf)
            else:
                surf.commit()
        try:
            self.d.roundtrip()
        except Exception:
            pass
        return rec

    def close(self):
        try:
            self.d.disconnect()
        except Exception:
            pass


def _d9_session(label, display_name):
    """A ready session, or None after printing why the test SKIPs."""
    if not _vendored_libweston_active():
        print(f"  SKIP [{label}] layer-chain grabs need the vendored "
              f"libweston; set QDWIN_USE_VENDORED_LIBWESTON=1")
        return None
    if WestonTest is None:
        print(f"  SKIP [{label}] no weston_test bindings (harness loaded no "
              f"test input module)")
        return None
    s = _D9Session(display_name)
    if not s.ok:
        print(f"  SKIP [{label}] {s.why} (harness loaded no test input "
              f"module?)")
        s.close()
        return None
    return s


def _errors_since(start):
    return [(m.group(1), int(m.group(3)), m.group(4))
            for m in _WL_ERR_RE.finditer(_read_captured_stderr_since(start))]


def _open_chain(s, nested=True):
    """L1 + grabbed P1 (+ grabbed submenu P2), each grab on a fresh press
    serial: the press on L1 opens P1, a press on P1 opens P2."""
    l1 = s.layer("L1", 50, 50)
    s.click(10, 10)
    p1 = s.popup("P1", 60, 0, layer=l1, grab=s.serial)
    if not nested:
        return l1, p1, None
    s.click(80, 20)
    p2 = s.popup("P2", 40, 0, parent=p1, grab=s.serial)
    return l1, p1, p2


def test_layer_chain_grab_live(display_name):
    """D9 positive path on a real grab: a submenu joins the root's grab, a
    press on it is delivered (no dismissal), and an outside press dismisses
    the chain topmost first."""
    label = "layer_chain_grab_live"
    s = _d9_session(label, display_name)
    if s is None:
        return True
    try:
        start = _stderr_size()
        _l1, _p1, _p2 = _open_chain(s)
        m = s.mark()
        s.click(120, 20)
        pressed = [e for e in s.since(m, "button") if e[2] == 1]
        if s.since(m, "popup_done") or not pressed or pressed[0][1] != "P2":
            print(f"  FAIL [{label}] press on the submenu was not delivered "
                  f"to it inside the grab: {s.since(m)}")
            return False
        m = s.mark()
        s.click(600, 300)
        done = [e[1] for e in s.since(m, "popup_done")]
        errs = _errors_since(start)
        if done != ["P2", "P1"] or errs:
            print(f"  FAIL [{label}] outside press: popup_done {done} "
                  f"(want ['P2', 'P1']), errors {errs}")
            return False
        print(f"  PASS [{label}] submenu press delivered; outside press "
              f"dismissed P2 then P1")
        return True
    finally:
        s.close()


def _admission(label, display_name, build, want):
    """Run build(s) on a fresh session and require exactly `want`: an
    (iface, code) protocol error, or ("popup_done", name) with no error."""
    s = _d9_session(label, display_name)
    if s is None:
        return True
    try:
        start = _stderr_size()
        m = build(s)
        errs = _errors_since(start)
        if want[0] == "popup_done":
            done = [e[1] for e in s.since(m, "popup_done")]
            if done == [want[1]] and not errs:
                return True
            print(f"  FAIL [{label}] want popup_done on {want[1]} and no "
                  f"error; got popup_done {done}, errors {errs}")
            return False
        if any(e[0] == want[0] and e[1] == want[1] for e in errs):
            return True
        print(f"  FAIL [{label}] want {want[0]} error {want[1]}; got "
              f"{errs}")
        return False
    finally:
        s.close()


def test_layer_chain_admission(display_name):
    """D9 follow-up 3: a submenu grab in a layer chain is answered per
    xdg_popup.grab, as the desktop seat grab answers it on a toplevel
    chain:
      not_topmost   parent grabbed but a submenu already joined above it
                    -> xdg_wm_base.not_the_topmost_popup (2)
      ungrabbed     parent is a popup that never took a grab
                    -> xdg_wm_base.invalid_popup_parent (3)
      nonchain      parent is an ungrabbed submenu of a grabbed root (the
                    stock path would install an owner-events grab here)
                    -> xdg_wm_base.invalid_popup_parent (3)
      dismissed     parent grabbed but already dismissed
                    -> the popup is dismissed at once (popup_done, no error)
      stale         parent topmost and live, stale serial
                    -> the layer-grab handler refuses: xdg_popup.invalid_grab
    """
    label = "layer_chain_admission"

    def not_topmost(s):
        _l1, p1, p2 = _open_chain(s)
        s.click(120, 20)
        m = s.mark()
        s.popup("P3", 0, 40, parent=p1, grab=s.serial, wait=False)
        return m

    def ungrabbed(s):
        l1 = s.layer("L1", 50, 50)
        p1 = s.popup("P1", 60, 0, layer=l1)
        s.click(80, 20)
        m = s.mark()
        s.popup("P2", 40, 0, parent=p1, grab=s.serial, wait=False)
        return m

    def nonchain(s):
        _l1, p1, _ = _open_chain(s, nested=False)
        s.click(80, 20)
        serial = s.serial
        p2 = s.popup("P2", 40, 0, parent=p1)
        m = s.mark()
        s.popup("P3", 0, 0, parent=p2, grab=serial, wait=False)
        return m

    def dismissed(s):
        l1, p1, _ = _open_chain(s, nested=False)
        s.click(600, 300)
        s.click(10, 10)
        m = s.mark()
        s.popup("P2", 40, 0, parent=p1, grab=s.serial, wait=False)
        return m

    def stale(s):
        _l1, p1, _ = _open_chain(s, nested=False)
        m = s.mark()
        s.popup("P2", 40, 0, parent=p1, grab=0, wait=False)
        return m

    cases = [
        ("not_topmost", not_topmost, ("xdg_wm_base", 2)),
        ("ungrabbed", ungrabbed, ("xdg_wm_base", 3)),
        ("nonchain", nonchain, ("xdg_wm_base", 3)),
        ("dismissed", dismissed, ("popup_done", "P2")),
        ("stale", stale, ("xdg_popup", 0)),
    ]
    if _d9_session_probe(label, display_name) is None:
        return True
    ok = True
    for name, build, want in cases:
        if _admission(f"{label}:{name}", display_name, build, want):
            print(f"  PASS [{label}:{name}] -> {want[0]} {want[1]}")
        else:
            ok = False
    return ok


def _d9_session_probe(label, display_name):
    s = _d9_session(label, display_name)
    if s is not None:
        s.close()
    return s


def test_layer_popup_grab_handler_registered(display_name):
    """plan3 post-review: positive discriminator for H1.

    Stock libweston has no layer-grab handler; xdg_popup.grab on a
    layer-parented popup posts XDG_POPUP_ERROR_INVALID_GRAB
    unconditionally. Patched libweston with qdwin registered calls the
    handler, which logs `qdwin: layer-popup grab handler registered`
    at compositor startup if the symbol was found. We use that log
    line as the discriminator — it is emitted exactly once per
    compositor run and is observable in the captured stderr.

    This is a lighter assertion than driving a real grab (which would
    need a wp_pointer + synthetic button press to produce a fresh
    serial); a follow-up VM GUI scenario exercises the live grab path.
    """
    label = "layer_popup_grab_handler_registered"
    if not _vendored_libweston_active():
        print(f"  SKIP [{label}] stock libweston-16 has no layer-popup grab "
              f"handler symbol; set QDWIN_USE_VENDORED_LIBWESTON=1 against "
              f"the vendored .so to exercise this")
        return True
    log_path = os.environ.get("QDWIN_COMPOSITOR_LOG")
    if not log_path or not os.path.exists(log_path):
        print(f"  SKIP [{label}] QDWIN_COMPOSITOR_LOG not set; run via "
              f"qdwin/run-protocol-tests.sh which captures the log")
        return True
    with open(log_path, "rb") as f:
        text = f.read().decode("utf-8", "replace")
    if "qdwin: layer-popup grab handler registered" in text:
        print(f"  PASS [{label}] handler was registered at compositor init")
        return True
    if "qdwin: layer-popup grab handler NOT registered" in text:
        print(f"  FAIL [{label}] handler not registered — patched "
              f"libweston symbol missing")
        return False
    print(f"  FAIL [{label}] neither registration log line found; "
          f"expected one of them")
    return False


def test_layer_popup_reposition(display_name):
    """plan3 M1: xdg_popup.reposition on a layer-parented popup must
    (1) NOT post a protocol error AND (2) actually take effect.

    Deep-review M3 tightening: the original test only checked (1). A
    server path that accepted reposition() but never scheduled a
    configure or sent xdg_popup.repositioned would still pass. Now we
    install event handlers and require the compositor to send both
    xdg_popup.repositioned(token=42) AND a follow-up
    xdg_surface.configure. Without either, fail.
    """
    label = "layer_popup_reposition"
    if not _vendored_libweston_active():
        print(f"  SKIP [{label}] layer-parented popup reposition only "
              f"schedules a configure on the qdistro-patched libweston; "
              f"set QDWIN_USE_VENDORED_LIBWESTON=1 against the vendored .so")
        return True
    d, st = with_globals(display_name)
    if not (st["xdg"] and st["shell"] and st["compositor"]):
        print(f"  SKIP [{label}] missing globals")
        try: d.disconnect()
        except: pass
        return True

    parent_surf = st["compositor"].create_surface()
    ls = st["shell"].get_layer_surface(
        parent_surf, None,
        ZwlrLayerShellV1.layer.top.value, "test-reposition-parent")
    ls.set_size(80, 80)
    ls.set_anchor(ZwlrLayerSurfaceV1.anchor.top.value
                  | ZwlrLayerSurfaceV1.anchor.left.value)
    ls.dispatcher["configure"] = lambda _ls, s, w, h: _ls.ack_configure(s)
    parent_surf.commit()
    d.roundtrip()

    positioner = st["xdg"].create_positioner()
    positioner.set_size(20, 20)
    positioner.set_anchor_rect(0, 0, 1, 1)
    popup_surf = st["compositor"].create_surface()
    popup_xs = st["xdg"].get_xdg_surface(popup_surf)
    popup = popup_xs.get_popup(None, positioner)
    if not hasattr(ls, "get_popup"):
        print(f"  SKIP [{label}] zwlr_layer_surface_v1 < v3 (no get_popup)")
        try: d.disconnect()
        except: pass
        return True
    ls.get_popup(popup)

    # Initial commit to trigger first configure/ack — required so the
    # popup is in the "committed" state when reposition runs (only then
    # does libweston schedule the next configure).
    saw_repositioned = [False]
    saw_configure_after_repos = [False]
    saw_initial_xdg_configure = [False]
    state = {"reposition_seen": False}
    def on_xdg_configure(_xs, serial):
        _xs.ack_configure(serial)
        if state["reposition_seen"]:
            saw_configure_after_repos[0] = True
        else:
            saw_initial_xdg_configure[0] = True
    popup_xs.dispatcher["configure"] = on_xdg_configure
    def on_repositioned(_p, token):
        if token == 42:
            saw_repositioned[0] = True
            state["reposition_seen"] = True
    # deep-review-2 H3: the previous guard
    #     if "repositioned" in popup.dispatcher.keys() if ... else True:
    # never installed the handler — pywayland's dispatcher is a fresh
    # dict, so "repositioned" is not a key before assignment and the
    # condition is False. Assign unconditionally, matching every other
    # dispatcher assignment in this file.
    popup.dispatcher["repositioned"] = on_repositioned
    popup_surf.commit()
    d.roundtrip(); d.roundtrip()

    # Reposition with a fresh positioner — must not post an error and
    # must produce the repositioned + configure event sequence.
    positioner2 = st["xdg"].create_positioner()
    positioner2.set_size(20, 20)
    positioner2.set_anchor_rect(10, 10, 5, 5)
    if not hasattr(popup, "reposition"):
        print(f"  SKIP [{label}] xdg_popup < v3 (no reposition)")
        try: d.disconnect()
        except: pass
        return True

    start = _stderr_size()
    popup.reposition(positioner2, 42)
    try:
        d.roundtrip(); d.roundtrip(); d.roundtrip()
    except Exception:
        pass
    captured = _read_captured_stderr_since(start)
    saw_err = bool(_WL_ERR_RE.search(captured))

    try: d.disconnect()
    except: pass

    if saw_err:
        print(f"  FAIL [{label}] reposition triggered protocol error: "
              f"{captured.strip()[:200]}")
        return False
    if not saw_repositioned[0]:
        print(f"  FAIL [{label}] xdg_popup.repositioned(42) not received "
              f"after reposition() — layer-parented popup did not "
              f"schedule a configure on the qdistro patch")
        return False
    if not saw_configure_after_repos[0]:
        print(f"  FAIL [{label}] xdg_surface.configure not received "
              f"after repositioned(42)")
        return False
    print(f"  PASS [{label}] reposition produced repositioned(42) + "
          f"xdg_surface.configure on layer-parented popup")
    return True


def main():
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <wayland-display>", file=sys.stderr)
        sys.exit(2)
    disp = sys.argv[1]

    # Redirect stderr to a capture file so libwayland's protocol-error
    # printouts can be read by expect_protocol_error.
    _setup_stderr_capture()

    tests = [
        ("handshake",                   test_handshake),
        ("invalid_layer",               test_invalid_layer),
        ("invalid_anchor",              test_invalid_anchor),
        ("invalid_keyboard_interactivity", test_invalid_keyboard_interactivity),
        ("invalid_exclusive_edge",      test_invalid_exclusive_edge),
        ("set_layer-out-of-range",      test_invalid_layer_set_layer),
        ("role-conflict",               test_role_conflict),
        ("null_parent_popup",           test_null_parent_popup),
        ("layer_popup_grab_stale_serial", test_layer_popup_grab_stale_serial),
        ("layer_popup_grab_handler_registered", test_layer_popup_grab_handler_registered),
        ("layer_popup_nested_grab_routed", test_layer_popup_nested_grab_routed),
        ("popup_double_construct_refused", test_popup_double_construct_refused),
        ("layer_chain_grab_live",       test_layer_chain_grab_live),
        ("layer_chain_admission",       test_layer_chain_admission),
        ("layer_popup_reposition",      test_layer_popup_reposition),
        ("v19_register_hotkey_live",    test_v19_register_hotkey_live),
    ]
    passed = failed = 0
    for name, fn in tests:
        print(f"--- {name} ---")
        try:
            ok = fn(disp)
        except Exception:
            traceback.print_exc()
            ok = False
        if ok: passed += 1
        else:  failed += 1

    _restore_stderr()
    print(f"\n=== {passed} passed, {failed} failed ===")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
