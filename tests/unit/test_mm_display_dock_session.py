"""Broker-side R9 display dock-session orchestration tests."""
from __future__ import annotations

import threading

import pytest

from multimachine.display_dock_session import (
    CarrierSupervisorEndpoint,
    DisplayDockSession,
    PanelControlEndpoint,
    PrimarySafetyEndpoint,
)
from multimachine.remote_display_slot import (
    ActionKind,
    DisplaySlotSpec,
    SlotPhase,
)


def grant(*, generation: int = 40, heartbeat_ms: int = 1000) -> dict:
    return {
        "primary_machine": "laptop",
        "peer_machine": "server",
        "trust_domain_id": "owner-machines",
        "generation": generation,
        "session_id": f"dock-{generation}",
        "slot_name": "rdp-0",
        "logical_x": 1280,
        "logical_y": 0,
        "width": 1280,
        "height": 800,
        "scale": 1,
        "allow_input": True,
        "lease_expires_at": 10_000,
        "heartbeat_ms": heartbeat_ms,
    }


class FakeShell:
    def __init__(self, events: list[str]):
        self.events = events
        self.enabled = False

    def perform(self, action, _grant) -> None:
        self.events.append(action.kind.value)
        self.enabled = action.kind is ActionKind.PRIMARY_ENABLE_OUTPUT

    def safe_state_confirmed(self, _slot_name: str) -> bool:
        return not self.enabled


class FakeInputGate:
    def __init__(self, events: list[str]):
        self.events = events
        self.enabled = False

    def perform(self, action, _grant) -> None:
        self.events.append("gate:" + action.kind.value)
        self.enabled = action.kind is ActionKind.PRIMARY_ENABLE_INPUT

    def safe_state_confirmed(self, _slot_name: str) -> bool:
        return not self.enabled


class FakeCarrierSession:
    def __init__(self, events: list[str], *, ready: bool = True,
                 on_close=lambda: None):
        self.events = events
        self.is_ready = ready
        self.is_alive = True
        self.on_close = on_close

    def ready(self) -> bool:
        return self.is_ready

    def alive(self) -> bool:
        return self.is_alive

    def close(self) -> None:
        self.events.append("carrier-session-close")
        self.is_alive = False
        self.on_close()


class Rig:
    def __init__(self, *, carrier_ready: bool = True):
        self.now = 100.0
        self.events: list[str] = []
        self.panel_safe = True
        self.panel_heartbeat_fails = False
        self.input_enabled = False
        self.carrier_safe = True
        self.carrier_session: FakeCarrierSession | None = None

        self.shell = FakeShell(self.events)
        self.local = PrimarySafetyEndpoint(
            set_input_enabled=self._input,
            synthesize_releases=lambda releases: self.events.append(
                "releases:" + str(len(releases))),
            clear_transfers=lambda: self.events.append("transfers-cleared"),
            safe_probe=lambda _slot: not self.input_enabled)
        self.panel = PanelControlEndpoint(
            command=self._panel_command,
            safe_probe=lambda _slot: self.panel_safe)

        def start(_grant):
            self.events.append("carrier-session-start")
            self.carrier_safe = False
            self.carrier_session = FakeCarrierSession(
                self.events, ready=carrier_ready,
                on_close=lambda: setattr(self, "carrier_safe", True))
            return self.carrier_session

        self.carrier = CarrierSupervisorEndpoint(
            start=start, safe_probe=lambda _slot: self.carrier_safe)
        self.audit = []
        self.owner = DisplayDockSession(
            slot=DisplaySlotSpec("rdp-0"), shell_layout=self.shell,
            primary_local=self.local, peer_panel=self.panel,
            carrier=self.carrier, clock=lambda: self.now,
            audit=self.audit.append)

    def _input(self, enabled: bool) -> None:
        self.events.append("input-on" if enabled else "input-off")
        self.input_enabled = enabled

    def _panel_command(self, kind: str) -> dict:
        self.events.append(f"panel-{kind}")
        if kind == "reserve":
            self.panel_safe = False
            return {"ok": True, "result": "reserved"}
        if kind == "heartbeat":
            if self.panel_heartbeat_fails:
                raise RuntimeError("peer lease expired")
            return {"ok": True, "result": "renewed"}
        if kind == "release":
            self.panel_safe = True
            return {"ok": True, "result": "released"}
        raise AssertionError(kind)


def test_owner_orders_attach_renewal_and_detach() -> None:
    rig = Rig()
    rig.owner.attach(grant())

    assert rig.owner.phase is SlotPhase.ACTIVE
    assert rig.owner.status().session_id == "dock-40"
    assert rig.events[:7] == [
        "panel-reserve",
        "panel-heartbeat",
        "primary-enable-output",
        "panel-heartbeat",
        "carrier-session-start",
        "panel-heartbeat",
        "input-on",
    ]
    assert rig.owner.status().next_heartbeat == 100.5

    attach_heartbeats = rig.events.count("panel-heartbeat")
    rig.now = 100.49
    assert rig.owner.poll() is False
    assert rig.events.count("panel-heartbeat") == attach_heartbeats
    rig.now = 100.5
    assert rig.owner.poll() is False
    assert rig.events[-1] == "panel-heartbeat"
    assert rig.events.count("panel-heartbeat") == attach_heartbeats + 1
    assert rig.owner.status().next_heartbeat == 101.0

    before_detach = len(rig.events)
    rig.owner.detach(40)
    assert rig.owner.phase is SlotPhase.DISABLED
    assert rig.owner.status().session_id is None
    assert rig.events[before_detach:] == [
        "input-off", "releases:0", "transfers-cleared",
        "carrier-session-close", "primary-disable-output", "panel-release",
    ]


def test_primary_safety_requires_one_input_adapter_and_delegates_gate() -> None:
    common = {
        "synthesize_releases": lambda _releases: None,
        "clear_transfers": lambda: None,
        "safe_probe": lambda _slot: True,
    }
    with pytest.raises(ValueError, match="exactly one real input gate"):
        PrimarySafetyEndpoint(**common)
    with pytest.raises(ValueError, match="exactly one real input gate"):
        PrimarySafetyEndpoint(
            set_input_enabled=lambda _enabled: None,
            input_gate=FakeInputGate([]), **common)

    events: list[str] = []
    gate = FakeInputGate(events)
    endpoint = PrimarySafetyEndpoint(input_gate=gate, **common)
    endpoint.perform(
        type("Action", (), {"kind": ActionKind.PRIMARY_ENABLE_INPUT})(),
        grant())
    assert events == ["gate:primary-enable-input"]
    assert not endpoint.safe_state_confirmed("rdp-0")
    endpoint.perform(
        type("Action", (), {"kind": ActionKind.PRIMARY_DISABLE_INPUT})(),
        grant())
    assert endpoint.safe_state_confirmed("rdp-0")


def test_carrier_loss_converges_to_failed_safe() -> None:
    rig = Rig()
    rig.owner.attach(grant())
    assert rig.carrier_session is not None
    rig.carrier_session.is_alive = False

    assert rig.owner.poll() is True
    assert rig.owner.phase is SlotPhase.FAILED_SAFE
    assert rig.input_enabled is False
    assert rig.shell.enabled is False
    assert rig.panel_safe is True
    assert rig.carrier.safe_state_confirmed("rdp-0")
    assert any(event.kind == "fail-safe" for event in rig.audit)

    rig.owner.reset_failed_safe()
    assert rig.owner.phase is SlotPhase.DISABLED
    rig.owner.attach(grant(generation=41))
    assert rig.owner.generation == 41


def test_peer_owned_expiry_makes_renewal_fail_safe_and_release_idempotent() -> None:
    rig = Rig()
    rig.owner.attach(grant())
    # The peer restored locally and made its generation terminal before the
    # source's scheduled renewal. The panel socket may now reject heartbeat;
    # teardown must accept independently observed safe state without resurrecting.
    rig.panel_safe = True
    rig.panel_heartbeat_fails = True

    with pytest.raises(Exception, match="peer panel heartbeat failed"):
        rig.owner.heartbeat(40)
    assert rig.owner.phase is SlotPhase.FAILED_SAFE
    assert "panel-release" not in rig.events
    assert rig.panel.active_generation is None


def test_failed_carrier_start_is_closed_and_full_attach_rolls_back() -> None:
    rig = Rig(carrier_ready=False)
    with pytest.raises(Exception, match="display attach failed"):
        rig.owner.attach(grant())
    assert rig.owner.phase is SlotPhase.FAILED_SAFE
    assert rig.carrier.session is None
    assert rig.carrier_safe
    assert rig.panel_safe
    assert not rig.shell.enabled


def test_panel_adapter_rejects_wrong_response_and_generation() -> None:
    endpoint = PanelControlEndpoint(
        command=lambda _kind: {"ok": True, "result": "wrong"},
        safe_probe=lambda _slot: True)
    with pytest.raises(RuntimeError, match="panel reserve failed"):
        endpoint.perform(
            type("Action", (), {"kind": ActionKind.PEER_BLANK_PANEL})(),
            grant())
    endpoint.active_generation = 40
    with pytest.raises(RuntimeError, match="another generation"):
        endpoint.heartbeat(41, grant())


def test_serve_always_detaches_on_stop() -> None:
    rig = Rig()
    rig.owner.attach(grant())
    stopped = threading.Event()
    stopped.set()
    rig.owner.serve(stopped)
    assert rig.owner.phase is SlotPhase.DISABLED
