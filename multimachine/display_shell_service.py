"""Authenticated D-Bus facade for qdshell display-layout transactions.

The service deliberately has only claim and acknowledge methods.  It does not
accept a caller-supplied layout.  The controller publishes requests after grant
verification; this facade authenticates the D-Bus sender as qdshell (or its
direct ``busctl`` child) before exposing or completing one.
"""
from __future__ import annotations

import json
from typing import Callable

from .display_shell_mailbox import (
    DisplayShellError,
    DisplayShellInputMailbox,
    DisplayShellMailbox,
)


DBUS_NAME = "org.qdistro.MultiMachineDisplay1"
DBUS_PATH = "/org/qdistro/MultiMachineDisplay1"


class DisplayShellServiceCore:
    """Pure authorization/error boundary used by the thin live D-Bus object."""

    def __init__(self, mailbox: DisplayShellMailbox, *,
                 trusted_sender: Callable[[str], bool],
                 input_mailbox: DisplayShellInputMailbox | None = None):
        self.mailbox = mailbox
        self.input_mailbox = input_mailbox
        self.trusted_sender = trusted_sender

    def claim_layout(self, sender: str) -> str:
        if not sender or not self.trusted_sender(sender):
            return ""
        request = self.mailbox.claim()
        if request is None:
            return ""
        return json.dumps(request, sort_keys=True, separators=(",", ":"))

    def acknowledge_layout(self, sender: str, *, request_id: str,
                           generation: int, result: str) -> bool:
        if not sender or not self.trusted_sender(sender):
            return False
        try:
            self.mailbox.acknowledge(
                request_id=request_id, generation=generation, result=result)
            return True
        except (DisplayShellError, TypeError, ValueError):
            return False

    def claim_input(self, sender: str) -> str:
        if (self.input_mailbox is None or not sender
                or not self.trusted_sender(sender)):
            return ""
        request = self.input_mailbox.claim()
        if request is None:
            return ""
        return json.dumps(request, sort_keys=True, separators=(",", ":"))

    def acknowledge_input(self, sender: str, *, request_id: str,
                          generation: int, result: str) -> bool:
        if (self.input_mailbox is None or not sender
                or not self.trusted_sender(sender)):
            return False
        try:
            self.input_mailbox.acknowledge(
                request_id=request_id, generation=generation, result=result)
            return True
        except (DisplayShellError, TypeError, ValueError):
            return False


def create_dbus_service(bus, bus_name, core: DisplayShellServiceCore):
    """Create the dbus-python object; imports stay out of host-only tests."""
    import dbus.service

    class DisplayShellDBus(dbus.service.Object):
        def __init__(self) -> None:
            super().__init__(bus_name, DBUS_PATH)

        @dbus.service.method(DBUS_NAME, in_signature="", out_signature="s",
                             sender_keyword="sender")
        def ClaimLayout(self, sender=None):
            return core.claim_layout(str(sender or ""))

        @dbus.service.method(DBUS_NAME, in_signature="sts", out_signature="b",
                             sender_keyword="sender")
        def AcknowledgeLayout(self, request_id, generation, result, sender=None):
            return core.acknowledge_layout(
                str(sender or ""), request_id=str(request_id),
                generation=int(generation), result=str(result))

        @dbus.service.method(DBUS_NAME, in_signature="", out_signature="s",
                             sender_keyword="sender")
        def ClaimInput(self, sender=None):
            return core.claim_input(str(sender or ""))

        @dbus.service.method(DBUS_NAME, in_signature="sts", out_signature="b",
                             sender_keyword="sender")
        def AcknowledgeInput(self, request_id, generation, result, sender=None):
            return core.acknowledge_input(
                str(sender or ""), request_id=str(request_id),
                generation=int(generation), result=str(result))

    return DisplayShellDBus()


def register_authenticated_service(mailbox: DisplayShellMailbox, *,
                                   shell_pid: int, bus=None,
                                   input_mailbox: DisplayShellInputMailbox | None = None):
    """Own the display bus name and pin calls to the enrolled qdshell pid."""
    if (not isinstance(shell_pid, int) or isinstance(shell_pid, bool)
            or shell_pid <= 0):
        raise ValueError("qdshell pid must be positive")
    import dbus
    import dbus.service

    from .mm_broker import dbus_sender_is_trusted_shell

    session_bus = bus or dbus.SessionBus()
    name = dbus.service.BusName(
        DBUS_NAME, bus=session_bus, allow_replacement=False,
        replace_existing=False, do_not_queue=True)
    core = DisplayShellServiceCore(
        mailbox, input_mailbox=input_mailbox,
        trusted_sender=lambda sender: dbus_sender_is_trusted_shell(
            session_bus, sender, shell_pid))
    service = create_dbus_service(session_bus, name, core)
    # Keep both BusName and object alive; losing either withdraws authority.
    return name, service
