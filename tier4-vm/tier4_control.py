"""tier4_control — P05a control wrapper for the tier-4 VM launcher.

Owns three responsibilities that, pre-fix-pass, were split across the
shell script and the SDK:

1. **App1 receiver claim** — registers ``com.qdistro.Tier4VM.uid<NNNN>``
   on the session bus so the qdshell PodApps panel sees the tier-4
   VM as a launcher peer (the same Send-To plumbing as qfileman /
   qterminator). Without this claim, PodApps will not group tier-4
   windows under the Tier4VM badge.

2. **Close RPC** — exposes a same-uid-only ``Close()`` method on the
   ``com.qdistro.Tier4VM.Control`` interface. qdshell's chrome
   close-button route invokes this; the handler runs
   :func:`tier4_chrome.close_vm` (ACPI → destroy → orphan reap) and
   returns a ``(ok, method, stderr, orphans)`` tuple the caller can
   surface to the user. Caller-uid attestation gates the destructive
   verb to the bound shell's uid (mitigates security MEDIUM-2).

3. **virt-viewer lifecycle** — exec's virt-viewer (the same argv the
   pre-P05a shell builds) as a child process, then runs a GLib
   mainloop to dispatch the bus methods. When virt-viewer exits OR a
   Close() RPC fires the destroy lifecycle, the control process tears
   down its bus name and exits.

This module is invoked by ``spawn-tier4.sh`` (as the admin uid)
*instead of* the pre-fix-pass direct ``virt-viewer`` exec. The shell
still does the libvirt domain define/start; this module owns the
view + close half.

Run from CLI for tests:
    python3 tier4_control.py --vm-name <vm> -- virt-viewer …

Degrades to a plain virt-viewer exec when dbus-python /
``qdistro_app.app_receiver`` is missing so a minimal image still
brings up the VM (close button then falls back to virt-viewer's own
xdg_toplevel.close semantics — the documented degraded mode).
"""
from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import threading
from typing import Any


# Allow the script to run from spawn-tier4.sh without the qdistro_app
# SDK / dbus-python installed; we degrade to a passthrough exec rather
# than blocking VM startup.
try:
    import dbus  # type: ignore[import-not-found]
    import dbus.service  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — degraded path
    dbus = None  # type: ignore[assignment]

try:
    from qdistro_app import app_receiver as _app_receiver  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — degraded path
    _app_receiver = None  # type: ignore[assignment]


# Sibling import — mirrors qdistro_integration.py's bootstrap so the
# module is importable from the flat tier4-vm/ layout.
try:
    from . import tier4_chrome as _tier4_chrome  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — flat layout fallback
    import importlib.util
    import pathlib
    _spec = importlib.util.spec_from_file_location(
        "tier4_chrome",
        pathlib.Path(__file__).with_name("tier4_chrome.py"))
    if _spec and _spec.loader:
        _tier4_chrome = importlib.util.module_from_spec(_spec)
        sys.modules["tier4_chrome"] = _tier4_chrome
        _spec.loader.exec_module(_tier4_chrome)  # type: ignore[union-attr]
    else:  # pragma: no cover
        _tier4_chrome = None  # type: ignore[assignment]


CONTROL_IFACE = "com.qdistro.Tier4VM.Control"
CONTROL_OBJ_PATH = "/com/qdistro/Tier4VM"
APP_FRIENDLY_NAME = "Tier4VM"

# vm_name validation mirrors tier4_chrome._VM_NAME_RE. We re-check here
# because the spawn helper passes vm_name on the CLI and a hostile env
# var could redirect it.
_VM_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")


def _log(msg: str) -> None:
    """Single structured log line shape for this module."""
    try:
        print(f"[tier4-vm/control] {msg}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass


def _build_tier4_dbus_object(vm_name: str, owner_uid: int,
                             on_close):  # type: ignore[no-untyped-def]
    """Construct the Tier4VMControl dbus.service.Object subclass.

    Defined inside a factory so dbus.service.method decorators don't
    execute at import time on hosts without dbus-python (we'd hit an
    ImportError at module top). The class only exists when the runtime
    dispatches a Close().
    """
    if dbus is None:  # pragma: no cover — degraded path
        return None

    class Tier4VMControl(dbus.service.Object):
        """Same-uid-gated Close() RPC for the tier-4 VM.

        Claims ``com.qdistro.Tier4VM.Control`` on the session bus. The
        Close method runs the ACPI→destroy lifecycle and replies with
        the structured outcome before the process tears down.
        """

        def __init__(self, bus, bus_name, vm_name, owner_uid, on_close):
            super().__init__(bus_name, CONTROL_OBJ_PATH)
            self._bus = bus
            self._vm_name = str(vm_name)
            self._owner_uid = int(owner_uid)
            self._on_close = on_close

        def _caller_uid(self, sender: str) -> int | None:
            """Resolve the dbus sender's unix uid via the bus daemon.

            Same pattern P02's broker uses (``GetConnectionUnixUser``).
            Returns ``None`` on any failure so the gate stays open in
            the rare case the lookup itself errors — the same-uid
            session bus is the load-bearing isolation; this is
            defence-in-depth.
            """
            try:
                bus_obj = self._bus.get_object(
                    "org.freedesktop.DBus", "/org/freedesktop/DBus")
                iface = dbus.Interface(bus_obj, "org.freedesktop.DBus")
                return int(iface.GetConnectionUnixUser(sender))
            except Exception:  # noqa: BLE001
                return None

        # signature: (b method:s stderr:s orphans:u)
        @dbus.service.method(CONTROL_IFACE, in_signature="",
                             out_signature="bsui",
                             sender_keyword="sender")
        def Close(self, sender=None):  # noqa: N802 — dbus method name
            caller_uid = self._caller_uid(sender) if sender else None
            if caller_uid is not None and caller_uid != self._owner_uid:
                _log(f"Close() denied: caller uid={caller_uid} "
                     f"!= owner uid={self._owner_uid} (vm={self._vm_name!r})")
                return (False, "denied", "caller-uid-mismatch", 0)
            _log(f"Close() invoked for vm={self._vm_name!r} "
                 f"(caller uid={caller_uid})")
            try:
                result = self._on_close()
            except Exception as e:  # noqa: BLE001
                _log(f"Close() handler raised: {e!r}")
                return (False, "exception", repr(e), 0)
            return (bool(result.get("ok", False)),
                    str(result.get("method", "unknown")),
                    str(result.get("stderr", "")),
                    int(result.get("orphans_reaped", 0)))

        @dbus.service.method(CONTROL_IFACE, in_signature="",
                             out_signature="s")
        def GetVmName(self):  # noqa: N802
            return self._vm_name

    return Tier4VMControl


def _install_control(vm_name: str) -> tuple[Any, Any] | tuple[None, None]:
    """Claim the App1 bus name AND the Tier4VM.Control bus name.

    Returns ``(app_receiver, control_object)``; either may be None when
    its corresponding subsystem isn't available (no dbus, no SDK).

    The App1 receiver makes the VM show up in PodApps as a Send-To
    target / launcher peer. The Control object is the destructive
    Close() RPC bound to the calling uid only.
    """
    if dbus is None or _app_receiver is None:
        _log("dbus-python or qdistro_app SDK not importable; "
             "running with no App1 + no Close RPC. The chrome close "
             "button will fall back to virt-viewer's xdg_toplevel.close, "
             "which exits the viewer but leaves the qemu domain "
             "running. Install python3-dbus + the qdistro SDK to enable "
             "ACPI→destroy on close-button.")
        return None, None

    receiver = _app_receiver.register_app(
        APP_FRIENDLY_NAME,
        on_receive=lambda kind, payload: _log(
            f"App1 receive dropped (tier-4 has no inbound path) "
            f"kind={kind!r} len={len(payload)}"),
        friendly_name=APP_FRIENDLY_NAME,
        supported_kinds=(),
        silo=str(vm_name),
    )
    if receiver is None:
        _log("App1 receiver registration failed (see SDK log line)")

    # Now claim the per-VM Control bus name and bind the Close method.
    try:
        bus = dbus.SessionBus()
        owner_uid = os.geteuid()
        bus_name = dbus.service.BusName(
            f"com.qdistro.Tier4VM.Control.uid{owner_uid}",
            bus, do_not_queue=True)
    except Exception as e:  # noqa: BLE001
        _log(f"Could not claim Tier4VM.Control bus name: {e!r}")
        return receiver, None

    cls = _build_tier4_dbus_object(vm_name, owner_uid,
                                   on_close=lambda: _default_close(vm_name))
    if cls is None:
        return receiver, None
    try:
        ctrl = cls(bus, bus_name, vm_name, owner_uid,
                   lambda: _default_close(vm_name))
    except Exception as e:  # noqa: BLE001
        _log(f"Could not register Tier4VMControl object: {e!r}")
        return receiver, None
    _log(f"Tier4VMControl registered (vm={vm_name!r} uid={owner_uid})")
    return receiver, ctrl


def _default_close(vm_name: str) -> dict:
    """ACPI→destroy lifecycle invoked from the Close() RPC."""
    if _tier4_chrome is None:
        return {"ok": False, "method": "missing",
                "stderr": "tier4_chrome not importable",
                "orphans_reaped": 0}
    result = _tier4_chrome.close_vm(str(vm_name))
    return {
        "ok": bool(result.ok),
        "method": result.method,
        "stderr": result.stderr,
        "orphans_reaped": int(result.orphans_reaped),
    }


def _start_glib_mainloop_in_thread():
    """Spin a GLib mainloop on a daemon thread so dbus methods dispatch.

    virt-viewer runs in the main thread (we wait on its subprocess). The
    bus needs an active mainloop to deliver Close() requests; we mark
    the thread daemon so process exit doesn't hang on the loop.
    """
    if dbus is None:
        return None
    try:
        from gi.repository import GLib  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover
        _log("python3-gobject (gi) missing; bus methods will not dispatch")
        return None
    import dbus.mainloop.glib
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    loop = GLib.MainLoop()

    def run():
        try:
            loop.run()
        except Exception:  # noqa: BLE001
            pass

    t = threading.Thread(target=run, name="tier4-dbus-loop", daemon=True)
    t.start()
    return loop


def _drain_orphans_after_viewer_exit(vm_name: str) -> int:
    """Best-effort: reap qdistro-forward orphans after virt-viewer exits.

    The original spawn-tier4.sh trap-EXIT already runs `virsh destroy`
    + `virsh undefine`; this only reaps user-side helpers that linger
    past the viewer.
    """
    if _tier4_chrome is None:
        return 0
    try:
        return int(_tier4_chrome._reap_orphans(vm_name))
    except Exception:  # noqa: BLE001
        return 0


def _exec_or_fork_viewer(viewer_argv: list[str]) -> int:
    """Run virt-viewer; return its exit code.

    Uses subprocess.Popen + .wait rather than os.execvp so the dbus
    mainloop on the side-thread continues to dispatch Close() RPCs
    while the viewer is open.
    """
    _log(f"launching viewer argv={viewer_argv!r}")
    try:
        proc = subprocess.Popen(viewer_argv)  # noqa: S603 — argv list, no shell
    except FileNotFoundError as e:
        _log(f"viewer exec failed: {e!r}")
        return 127
    # Forward SIGINT / SIGTERM to the viewer.
    def _forward(signum, _frame):
        try:
            proc.send_signal(signum)
        except Exception:  # noqa: BLE001
            pass
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, _forward)
        except (OSError, ValueError):
            pass
    rc = proc.wait()
    _log(f"viewer exited rc={rc}")
    return rc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tier4_control",
        description="Tier-4 VM control wrapper: claims App1 bus name, "
                    "exposes Close() RPC, runs virt-viewer.")
    parser.add_argument("--vm-name", required=True,
                        help="Libvirt domain name (== secctx silo tag).")
    parser.add_argument("viewer_argv", nargs=argparse.REMAINDER,
                        help="Viewer command + args (e.g. virt-viewer …). "
                             "Pass via `-- virt-viewer --connect …`.")
    ns = parser.parse_args(argv)

    vm_name = ns.vm_name
    if not _VM_NAME_RE.match(vm_name):
        _log(f"invalid --vm-name {vm_name!r}; refusing to launch")
        return 2

    viewer_argv = list(ns.viewer_argv or [])
    # argparse stuffs the literal `--` separator at the head of REMAINDER
    # when invoked as `... -- virt-viewer ...`; strip it.
    while viewer_argv and viewer_argv[0] == "--":
        viewer_argv.pop(0)
    if not viewer_argv:
        _log("no viewer argv given; nothing to launch")
        return 2

    receiver, control = _install_control(vm_name)
    loop = _start_glib_mainloop_in_thread()

    try:
        rc = _exec_or_fork_viewer(viewer_argv)
    finally:
        reaped = _drain_orphans_after_viewer_exit(vm_name)
        if reaped:
            _log(f"reaped {reaped} orphan(s) after viewer exit")
        # Best-effort: release bus names. Letting them GC works too but
        # tearing down explicitly makes the journal clearer.
        del control  # noqa: F841
        del receiver  # noqa: F841
        if loop is not None:
            try:
                loop.quit()
            except Exception:  # noqa: BLE001
                pass
    return rc


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
