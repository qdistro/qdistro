"""Installed primary owner for one pre-created remote-display slot."""
from __future__ import annotations

import argparse
import os
import signal
import socket
import stat
import subprocess
import threading
import time
from collections.abc import Mapping
from pathlib import Path

from .display_dock_rpc import (
    JsonEndpointClient,
    RpcCarrierFactory,
    RpcPanelEndpoint,
    RpcPrimarySafetyEndpoint,
)
from .display_dock_service import (
    DisplayDockServiceCore,
    DockServiceError,
    SecureStatusStore,
    serve_control_socket,
)
from .display_dock_session import (
    CarrierSupervisorEndpoint,
    DisplayDockSession,
)
from .display_shell_mailbox import (
    DisplayShellInputMailbox,
    DisplayShellMailbox,
)
from .display_shell_service import register_authenticated_service
from .mm_display_authority import DisplayGrantVerifier
from .mm_session_launcher import _read_pinned_authority_key
from .remote_display_slot import (
    ActionKind,
    DisplaySlotSpec,
    SlotAction,
)


DEFAULT_CONTROL = "/run/qdistro-mm-display-dock/control.sock"
DEFAULT_STATUS = "/var/lib/qdistro-mm-display-dock/status.json"
DEFAULT_SAFETY = "/run/qdistro/mm-display-primary-safety.sock"
DEFAULT_PANEL = "/run/qdistro/mm-display-peer-panel.sock"
DEFAULT_CARRIER = "/run/qdistro/mm-display-carrier-supervisor.sock"


def resolve_shell_pid(unit: str = "qdshell.service") -> int:
    if unit != "qdshell.service":
        raise ValueError("display dock shell unit must be qdshell.service")
    result = subprocess.run(
        ["systemctl", "--user", "show", "--property=MainPID", "--value", unit],
        check=False, capture_output=True, text=True, timeout=5)
    try:
        pid = int(result.stdout.strip())
    except ValueError as exc:
        raise DockServiceError("cannot resolve qdshell systemd pid") from exc
    if result.returncode != 0 or pid <= 0:
        raise DockServiceError("qdshell systemd unit is not running")
    return pid


def prepare_control_listener(path: str, *, controller_uid: int) -> socket.socket:
    if (not isinstance(controller_uid, int) or isinstance(controller_uid, bool)
            or controller_uid < 0):
        raise ValueError("display dock controller uid is invalid")
    endpoint = Path(path)
    parent = endpoint.parent
    info = parent.stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o022):
        raise DockServiceError("display dock runtime directory is unsafe")
    try:
        existing = endpoint.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if (not stat.S_ISSOCK(existing.st_mode)
                or existing.st_uid != os.geteuid()):
            raise DockServiceError("display dock control path is unsafe")
        endpoint.unlink()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(path)
        # The service owner can create the socket; the root pairing controller
        # may connect despite mode 0600 and is still checked with SO_PEERCRED.
        os.chmod(path, 0o600)
        listener.listen(8)
        return listener
    except Exception:
        listener.close()
        endpoint.unlink(missing_ok=True)
        raise


class PrimaryLocalEndpoint:
    """Combine qdwin input admission with fixed primary-safety RPC."""

    def __init__(self, input_mailbox: DisplayShellInputMailbox,
                 safety: RpcPrimarySafetyEndpoint):
        self.input_mailbox = input_mailbox
        self.safety = safety

    def perform(self, action: SlotAction, grant: Mapping) -> None:
        if action.kind in {
            ActionKind.PRIMARY_ENABLE_INPUT,
            ActionKind.PRIMARY_DISABLE_INPUT,
        }:
            self.input_mailbox.perform(action, grant)
        else:
            self.safety.perform(action, grant)

    def safe_state_confirmed(self, slot_name: str) -> bool:
        return (self.input_mailbox.safe_state_confirmed(slot_name)
                and self.safety.safe_state_confirmed(slot_name))


def recovery_grant(previous: Mapping | None, *, slot_name: str) -> dict:
    if previous is not None and previous.get("recovery_grant") is not None:
        return dict(previous["recovery_grant"])
    now = int(time.time())
    generation = 1 if previous is None else max(1, int(previous["generation"]))
    return {
        "primary_machine": "recovery-primary",
        "peer_machine": "recovery-peer",
        "trust_domain_id": "recovery",
        "generation": generation,
        "session_id": "startup-recovery",
        "slot_name": slot_name,
        "logical_x": 0,
        "logical_y": 0,
        "width": 1,
        "height": 1,
        "scale": 1,
        "allow_input": False,
        "lease_expires_at": now + 30,
        "heartbeat_ms": 1000,
    }


def build_service(args, *, shell_pid: int | None = None,
                  clock=time.time):
    """Build the daemon graph; kept injectable for host deployment tests."""
    layout = DisplayShellMailbox(clock=clock, request_timeout=15)
    input_mailbox = DisplayShellInputMailbox(clock=clock, request_timeout=15)
    safety_client = JsonEndpointClient(
        args.safety_socket, role="primary-safety")
    panel_client = JsonEndpointClient(args.panel_socket, role="peer-panel")
    carrier_client = JsonEndpointClient(args.carrier_socket, role="carrier")
    safety = RpcPrimarySafetyEndpoint(safety_client)
    panel = RpcPanelEndpoint(panel_client)
    carrier_factory = RpcCarrierFactory(carrier_client)
    carrier = CarrierSupervisorEndpoint(
        start=carrier_factory.start, safe_probe=carrier_factory.safe)
    owner = DisplayDockSession(
        slot=DisplaySlotSpec(
            args.slot_name, max_width=args.max_width,
            max_height=args.max_height, max_scale=args.max_scale),
        shell_layout=layout,
        primary_local=PrimaryLocalEndpoint(input_mailbox, safety),
        peer_panel=panel, carrier=carrier, clock=clock)
    public_key = _read_pinned_authority_key()
    verifier = DisplayGrantVerifier.from_public_bytes(
        public_key, role="primary", local_machine_id=args.local_machine_id,
        now=clock)

    def verify(receipt, session_id, secret):
        return verifier.verify(
            receipt, session_id=session_id, carrier_secret=secret)

    def recover(previous: Mapping | None) -> bool:
        grant = recovery_grant(previous, slot_name=args.slot_name)
        # Safety-off actions are deliberately ordered before carrier/output
        # retirement. Endpoint `recover` is idempotent and must independently
        # confirm its local resources are safe.
        input_mailbox.perform(
            SlotAction(ActionKind.PRIMARY_DISABLE_INPUT), grant)
        if safety_client.request("recover", grant) != "safe":
            return False
        if carrier_client.request("recover", grant) != "safe":
            return False
        layout.perform(SlotAction(ActionKind.PRIMARY_DISABLE_OUTPUT), grant)
        if panel_client.request("recover", grant) != "safe":
            return False
        return (input_mailbox.safe_state_confirmed(args.slot_name)
                and layout.safe_state_confirmed(args.slot_name))

    core = DisplayDockServiceCore(
        owner=owner, controller_uid=args.controller_uid,
        verify_attach=verify, store=SecureStatusStore(Path(args.status_path)),
        recover=recover, clock=clock)
    registration = register_authenticated_service(
        layout, shell_pid=shell_pid or resolve_shell_pid(),
        input_mailbox=input_mailbox)
    return core, registration


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - live daemon
    ap = argparse.ArgumentParser(prog="qdistro-mm-display-dock")
    ap.add_argument("--local-machine-id", required=True)
    ap.add_argument("--slot-name", default="rdp-0")
    ap.add_argument("--controller-uid", type=int, default=0)
    ap.add_argument("--control-path", default=DEFAULT_CONTROL)
    ap.add_argument("--status-path", default=DEFAULT_STATUS)
    ap.add_argument("--safety-socket", default=DEFAULT_SAFETY)
    ap.add_argument("--panel-socket", default=DEFAULT_PANEL)
    ap.add_argument("--carrier-socket", default=DEFAULT_CARRIER)
    ap.add_argument("--max-width", type=int, default=7680)
    ap.add_argument("--max-height", type=int, default=4320)
    ap.add_argument("--max-scale", type=int, default=2)
    args = ap.parse_args(argv)

    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    DBusGMainLoop(set_as_default=True)
    core, registration = build_service(args)
    assert registration
    listener = prepare_control_listener(
        args.control_path, controller_uid=args.controller_uid)
    stop = threading.Event()
    loop = GLib.MainLoop()

    def request_stop(_signum=None, _frame=None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    failure: list[BaseException] = []

    def run() -> None:
        try:
            serve_control_socket(core, listener, stop)
        except BaseException as exc:
            failure.append(exc)
        finally:
            GLib.idle_add(loop.quit)

    worker = threading.Thread(target=run, name="display-dock-control")
    worker.start()
    try:
        GLib.timeout_add(100, lambda: not stop.is_set())
        loop.run()
    finally:
        stop.set()
        worker.join(timeout=35)
        listener.close()
        Path(args.control_path).unlink(missing_ok=True)
    if worker.is_alive():
        raise DockServiceError("display dock control thread did not stop")
    if failure:
        raise failure[0]
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
