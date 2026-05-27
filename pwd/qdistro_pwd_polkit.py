"""Polkit gate for the qdistro password-manager daemon.

spec/13 Phase-8.2 — `UnlockVault` for non-admin callers must be
authorised by an admin polkit agent before the master key is unsealed.
This module wraps `org.freedesktop.PolicyKit1.Authority.CheckAuthorization`
so the daemon can decide:

  - admin uid (1000)            → bypass polkit entirely (unconditional ALLOW)
  - non-admin uid + agent reachable
                                → run the polkit prompt; honour the result
  - non-admin uid + no agent registered
                                → fail closed with PolkitNoAgent
  - QDISTRO_PWD_POLKIT_REQUIRED=0
                                → bypass entirely (development / bring-up;
                                  noisily logged so the operator notices)

The actual prompt is rendered by whatever polkit AuthenticationAgent
is registered with polkitd in the admin's session — typically
lxqt-policykit, polkit-gnome, or (future) the qdistro admin agent
(spec/25 §"admin-approval-app"). This daemon-side module doesn't
care which agent renders the prompt; it just calls polkit and reads
the verdict.

Action: `org.qdistro.pwd.unlock`
Action details:
  - "vault"      = vault name
  - "caller-pid" = caller PID
  - "caller-uid" = caller UID
  - "caller-exe" = best-effort /proc/<pid>/exe (informational; the
                   daemon already validates this elsewhere)
"""
from __future__ import annotations

import os
import pwd as _pwd_mod
from typing import Any

import dbus


POLKIT_BUS = "org.freedesktop.PolicyKit1"
POLKIT_PATH = "/org/freedesktop/PolicyKit1/Authority"
POLKIT_IFC = "org.freedesktop.PolicyKit1.Authority"

try:
    ADMIN_UID = _pwd_mod.getpwnam(
        os.environ.get("QDISTRO_ADMIN_USER", "admin")).pw_uid
except KeyError:
    ADMIN_UID = 1000

ACTION_UNLOCK = "org.qdistro.pwd.unlock"

# CheckAuthorization flags. Bit 1 = AllowUserInteraction (let polkit
# trigger the registered agent for an interactive auth dialog).
CHECK_FLAG_ALLOW_USER_INTERACTION = 0x1


class PolkitNoAgent(Exception):
    """Raised when CheckAuthorization succeeds at the protocol level
    but `challenge` came back True with no agent able to respond. The
    daemon maps this to a PolicyError with a hint to start an admin
    polkit agent."""


class PolkitDenied(Exception):
    """Raised when polkit returned authorized=False. Caller's
    interactive auth was rejected (wrong password / fingerprint, agent
    cancelled, etc)."""


def is_required() -> bool:
    """Whether the polkit gate is on. Default ON; opt-OUT via
    `QDISTRO_PWD_POLKIT_REQUIRED=0` for fresh-VM bootstrap and tests
    that drive UnlockVault from a non-admin uid without a polkit
    agent in the loop."""
    return os.environ.get("QDISTRO_PWD_POLKIT_REQUIRED", "1") != "0"


def check_unlock(uid: int, pid: int, vault: str, caller_exe: str = "",
                 *, bus: dbus.Bus | None = None,
                 timeout: float = 60.0) -> tuple[bool, str]:
    """Ask polkit whether `uid/pid` may unlock `vault`. Returns
    `(allowed, reason)`. `reason` is "admin-bypass" / "polkit-allow" /
    "polkit-allow-keep" / "polkit-cancelled" / "polkit-not-required".

    Raises:
      - PolkitNoAgent if polkit asks for a challenge but no agent is
        available to render the prompt.
      - PolkitDenied if polkit cancelled / refused without a challenge.
      - dbus.DBusException for protocol-level failures (polkitd
        unreachable, bad action id).
    """
    if not is_required():
        return True, "polkit-not-required"
    if int(uid) == ADMIN_UID:
        return True, "admin-bypass"

    if bus is None:
        bus = dbus.SystemBus()
    proxy = bus.get_object(POLKIT_BUS, POLKIT_PATH)
    ifc = dbus.Interface(proxy, POLKIT_IFC)

    # Subject = unix-process by pid + start_time (start_time = 0
    # tolerates "any start time", which polkit accepts at the cost of
    # a small race window if pids wrap during the prompt).
    subject = ("unix-process", {
        "pid":        dbus.UInt32(int(pid)),
        "start-time": dbus.UInt64(0),
    })
    details = {
        "vault":      str(vault),
        "caller-pid": str(int(pid)),
        "caller-uid": str(int(uid)),
        "caller-exe": str(caller_exe),
    }
    cancellation_id = ""
    result = ifc.CheckAuthorization(
        subject, ACTION_UNLOCK, details,
        dbus.UInt32(CHECK_FLAG_ALLOW_USER_INTERACTION),
        cancellation_id,
        timeout=timeout,
    )
    # result is a triple (is_authorized, is_challenge, details)
    is_authorized = bool(result[0])
    is_challenge = bool(result[1])
    res_details = result[2] if len(result) > 2 else {}
    if is_authorized:
        # If a session-keep flag is set, return a slightly different
        # reason so the audit log shows whether the prompt was cached.
        keep = bool(res_details and "polkit.retains_authorization_after_challenge"
                    in dict(res_details))
        return True, "polkit-allow-keep" if keep else "polkit-allow"
    if is_challenge:
        # polkit returned (False, True, ...) when it would have
        # prompted but no agent was reachable.
        raise PolkitNoAgent(
            "polkit requested user interaction but no admin "
            "AuthenticationAgent is registered for this session")
    raise PolkitDenied("polkit refused the unlock request")
