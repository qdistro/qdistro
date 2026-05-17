"""Fingerprint verification helper for the qdistro-pwd daemon.

Wraps a single net.reactivated.Fprint.Device.VerifyStart cycle into a
synchronous (matched, reason) return so the daemon's AutoUnlockPortalKeys
and direct UnlockVaultFprint flows can gate on a successful touch
without each method re-implementing the dbus dance.

The polkit agent (polkit/qdistro_polkit_agent.py) ships its own
near-identical helper for the polkit BeginAuthentication path; that one
predates this module and stays put — refactoring an already-shipping
agent for a one-call DRY would risk regressions for no functional gain.
The two helpers track the same fprintd surface and can converge later.

Returns:
    (matched, reason)     where reason is a short tag suitable for
                          the audit log: ``fprint-match``, ``fprint:timeout``,
                          ``fprintd-claim:<msg>``, ``fprintd-no-device``,
                          ``fprintd-import-failed``, etc.

This module never raises on fprintd failure. The caller decides whether
to fail-closed or fall through. ``QDISTRO_PORTAL_FPRINT_TIMEOUT_S``
overrides the default verify timeout (30s).
"""
from __future__ import annotations

import os
import threading
from typing import Any

_FPRINTD_BUS_NAME = "net.reactivated.Fprint"
_FPRINTD_MGR_PATH = "/net/reactivated/Fprint/Manager"
_FPRINTD_MGR_IFACE = "net.reactivated.Fprint.Manager"
_FPRINTD_DEV_IFACE = "net.reactivated.Fprint.Device"

DEFAULT_TIMEOUT_S = int(os.environ.get("QDISTRO_PORTAL_FPRINT_TIMEOUT_S", "30"))


def is_fprintd_available(system_bus: Any) -> bool:
    """Cheap probe: does fprintd respond on the system bus AND have a
    default device? Returns False on either ImportError (no dbus-python)
    or a DBusException (service unreachable / no enrolled finger).
    """
    try:
        import dbus  # noqa: F401  — keep guard symmetric with verify()
    except ImportError:
        return False
    try:
        mgr_obj = system_bus.get_object(_FPRINTD_BUS_NAME, _FPRINTD_MGR_PATH)
        import dbus as _dbus
        mgr = _dbus.Interface(mgr_obj, _FPRINTD_MGR_IFACE)
        dev_path = str(mgr.GetDefaultDevice())
        return bool(dev_path)
    except Exception:  # noqa: BLE001 — D-Bus / fprintd both raise here
        return False


def verify(user: str, system_bus: Any,
           timeout_s: int | None = None) -> tuple[bool, str]:
    """Run a fprintd VerifyStart cycle for ``user``. Blocks until a
    VerifyStatus signal arrives or ``timeout_s`` elapses.

    The user must already have a finger enrolled. The caller's polkit
    rights must allow ``net.reactivated.fprint.device.verify`` for
    ``user`` — for the qdistro-pwd daemon that means a polkit rule
    granting the qdistro-pwd uid that action (shipped via
    pwd/qdistro-pwd-fprint.rules).
    """
    timeout_s = int(timeout_s if timeout_s is not None else DEFAULT_TIMEOUT_S)
    try:
        import dbus
    except ImportError:
        return False, "fprintd-import-failed"

    try:
        mgr_obj = system_bus.get_object(_FPRINTD_BUS_NAME, _FPRINTD_MGR_PATH)
        mgr = dbus.Interface(mgr_obj, _FPRINTD_MGR_IFACE)
        try:
            dev_path = mgr.GetDefaultDevice()
        except dbus.DBusException as e:
            return False, f"fprintd-no-device:{e.get_dbus_message()}"
        dev_obj = system_bus.get_object(_FPRINTD_BUS_NAME, dev_path)
        dev = dbus.Interface(dev_obj, _FPRINTD_DEV_IFACE)
        try:
            dev.Claim(user)
        except dbus.DBusException as e:
            return False, f"fprintd-claim:{e.get_dbus_message()}"
    except dbus.DBusException as e:
        return False, f"fprintd-init:{e.get_dbus_message()}"

    done = threading.Event()
    result = {"status": "", "matched": False}

    def on_verify_status(status, finished):
        result["status"] = str(status)
        if str(status) == "verify-match":
            result["matched"] = True
        if bool(finished) or str(status) == "verify-match":
            done.set()

    sig = system_bus.add_signal_receiver(
        on_verify_status, signal_name="VerifyStatus",
        dbus_interface=_FPRINTD_DEV_IFACE, path=dev_path)
    try:
        dev.VerifyStart("any")
        done.wait(timeout=timeout_s)
    except dbus.DBusException as e:
        return False, f"fprintd-verify:{e.get_dbus_message()}"
    finally:
        try:
            dev.VerifyStop()
        except Exception:  # noqa: BLE001
            pass
        try:
            dev.Release()
        except Exception:  # noqa: BLE001
            pass
        try:
            sig.remove()
        except Exception:  # noqa: BLE001
            pass

    if result["matched"]:
        return True, "fprint-match"
    return False, f"fprint:{result['status'] or 'timeout'}"


def admin_username(admin_uid: int) -> str:
    """Resolve the admin uid into a username via the system passwd db.
    Falls back to "admin" if the lookup
    fails so an offline / chroot env still picks a meaningful default.
    """
    try:
        import pwd  # POSIX user db; not the qdistro pwd package.
        return pwd.getpwuid(int(admin_uid)).pw_name
    except (KeyError, ImportError, OSError):
        return "admin"
