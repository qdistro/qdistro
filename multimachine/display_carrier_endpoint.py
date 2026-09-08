"""Sealed-config endpoint for one R9 RDP display-carrier generation."""
from __future__ import annotations

import argparse
import json
import logging
import signal
import socket
import subprocess
import threading
from pathlib import Path

from .display_carrier import serve_peer_once, serve_primary_once
from .display_dock_rpc import (
    DisplayDockRpcError,
    peer_uid,
    prepare_endpoint_listener,
    receive_endpoint_request,
    send_endpoint_response,
)
from .mm_display_carrier_launcher import parse_carrier_config
from .mm_session_launcher import _read_owned_json_fd


_ACTIONS = frozenset({"open", "alive", "close", "safe", "recover"})


def _exit_on_signal(_signum, _frame) -> None:
    raise SystemExit(0)


def _systemctl(action: str, unit: str) -> None:
    result = subprocess.run(
        ["systemctl", action, unit], check=False, timeout=15,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode != 0:
        raise RuntimeError(f"display carrier cannot {action} client unit")


class PrimaryCarrierAgent:
    """One sealed generation's fixed carrier-supervisor control surface."""

    def __init__(self, runtime, listener: socket.socket):
        self.runtime = runtime
        self.listener = listener
        self.worker: threading.Thread | None = None
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.error: BaseException | None = None
        self.opened = False

    def _run(self) -> None:
        try:
            serve_primary_once(
                self.runtime.identity, listener=self.listener,
                backend_unix_path=self.runtime.target["backend_unix_path"],
                on_ready=self.ready.set, stop_requested=self.stop.is_set)
        except BaseException as exc:
            self.error = exc

    def open(self) -> str:
        if self.worker is not None or self.opened:
            return "rejected"
        self.opened = True
        self.stop.clear()
        self.ready.clear()
        self.error = None
        self.worker = threading.Thread(
            target=self._run, name="display-carrier-relay", daemon=True)
        self.worker.start()
        if not self.ready.wait(20):
            self.stop.set()
            self.worker.join(timeout=2)
            return "failed"
        return "ready"

    def alive(self) -> str:
        return ("alive" if self.worker is not None and self.worker.is_alive()
                and self.ready.is_set() and self.error is None else "dead")

    def close(self) -> str:
        worker = self.worker
        if worker is not None:
            self.stop.set()
            worker.join(timeout=3)
            if worker.is_alive():
                return "failed"
        self.worker = None
        self.ready.clear()
        return "closed"

    def safe(self) -> str:
        return "safe" if self.worker is None else "unsafe"

    def perform(self, action: str) -> str:
        if action == "open":
            return self.open()
        if action == "alive":
            return self.alive()
        if action == "close":
            return self.close()
        if action == "recover":
            return "safe" if self.close() == "closed" else "unsafe"
        return self.safe()


def serve_primary_agent(runtime, listener: socket.socket) -> int:
    path = runtime.target["control_unix_path"]
    control = prepare_endpoint_listener(
        path, uid=runtime.target["control_uid"],
        gid=runtime.target["control_gid"])
    agent = PrimaryCarrierAgent(runtime, listener)
    try:
        while True:
            client, _address = control.accept()
            with client:
                client.settimeout(25)
                if peer_uid(client) != runtime.target["control_uid"]:
                    send_endpoint_response(
                        client, ok=False, result="unauthorized-controller")
                    continue
                try:
                    action = receive_endpoint_request(
                        client, role="carrier",
                        generation=runtime.identity.binding.generation,
                        session_id=runtime.identity.binding.session_id,
                        slot_name=runtime.identity.binding.slot_name,
                        actions=_ACTIONS)
                except DisplayDockRpcError:
                    send_endpoint_response(
                        client, ok=False, result="invalid-request")
                    continue
                result = agent.perform(action)
                expected = {
                    "open": "ready", "alive": "alive", "close": "closed",
                    "safe": "safe", "recover": "safe",
                }[action]
                send_endpoint_response(
                    client, ok=result == expected, result=result)
    finally:
        agent.close()
        control.close()
        Path(path).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - subprocess
    ap = argparse.ArgumentParser(prog="qdistro-mm-display-carrier")
    ap.add_argument("--config-fd", type=int, required=True)
    args = ap.parse_args(argv)
    runtime = parse_carrier_config(
        _read_owned_json_fd(args.config_fd, "display carrier config"))
    signal.signal(signal.SIGTERM, _exit_on_signal)
    listener = socket.socket(fileno=runtime.listener_fd)
    try:
        if runtime.identity.role == "primary":
            return serve_primary_agent(runtime, listener)
        else:
            unit = runtime.target["client_unit"]
            try:
                result = serve_peer_once(
                    runtime.identity, local_listener=listener,
                    primary_host=runtime.target["primary_host"],
                    primary_port=runtime.target["primary_port"],
                    on_authenticated=lambda: _systemctl("start", unit))
            finally:
                try:
                    _systemctl("stop", unit)
                except Exception:
                    # Teardown failure must be visible, but must not replace
                    # the carrier/TLS failure that caused this finally block.
                    logging.exception(
                        "display carrier could not stop peer client unit")
        print(json.dumps({
            "closed_by": result.closed_by,
            "left_to_right": result.left_to_right,
            "right_to_left": result.right_to_left,
        }, sort_keys=True), flush=True)
        return 0
    finally:
        listener.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
