"""Dry-run tests for the compositor-boundary direct-claimant gate (A1, session 7;
codex impl-17/impl-18).

A ``MockClaimantBackend`` simulates the single VM WITHOUT libvirt/wayland: qdwin
"spawns" ``qdwin-stream-claimant`` in place of ``qdistro-forward``; the claimant
claims the per-stream token and injects a button press that lands ONLY on the
exported marker's per-stream seat (``qdwin-stream-<rdp_port>``), never the local
sentinel. Flags model each failure/dishonesty mode so the gate's verdict is pinned:
a missing negative-protocol outcome, a vacuous sentinel zero, a press on the wrong
seat, a dead marker, etc. The live gate (QciVMBackend) runs the identical
orchestration end-to-end against a real headless qdwin.
"""
from __future__ import annotations

from multimachine.harness.scenario import run_direct_claimant_slice
from multimachine.harness.topology import Topology

RDP_PORT = 43210
STREAM_SEAT = f"qdwin-stream-{RDP_PORT}"


def _seat(name, gname, *, button_press=0, key_press=0, pointer_motion=0):
    return {"name": gname, "seat_name": name, "has_pointer": 1, "has_keyboard": 1,
            "pointer_enter": 1 if pointer_motion else 0,
            "pointer_motion": pointer_motion, "button_press": button_press,
            "keyboard_enter": 0, "key_press": key_press, "last_x": 0, "last_y": 0}


def _marker_tel(label, seats):
    tbp = sum(int(s.get("button_press", 0)) for s in seats)
    tkp = sum(int(s.get("key_press", 0)) for s in seats)
    tpm = sum(int(s.get("pointer_motion", 0)) for s in seats)
    return {"label": label, "output_id": 1, "generation": 7,
            "seats_seen": len(seats), "seats": seats,
            "totals": {"pointer_enter": 0, "pointer_motion": tpm,
                       "button_press": tbp, "keyboard_enter": 0, "key_press": tkp}}


class MockClaimantBackend:
    def __init__(self, *, claim_real=True, already_claimed=True, invalid_token=True,
                 inject_sent=True, deliver=True, leak_sentinel=False,
                 wrong_seat=False, dead_sentinel=False, dead_exported=False,
                 rdp_port=RDP_PORT):
        self.flags = dict(
            claim_real=claim_real, already_claimed=already_claimed,
            invalid_token=invalid_token, inject_sent=inject_sent, deliver=deliver,
            leak_sentinel=leak_sentinel, wrong_seat=wrong_seat,
            dead_sentinel=dead_sentinel, dead_exported=dead_exported)
        self.rdp_port = rdp_port
        self.calls: list[tuple] = []
        self._tel: dict[str, dict] = {}
        self._paths: dict[str, str] = {}

    def spin(self, name): self.calls.append(("spin", name)); return name
    def destroy(self, vm): self.calls.append(("destroy", vm))
    def source_alive(self, vm): return not self.flags["dead_exported"]

    def setup_claimant_source(self, vm, *, generation, width, height,
                              exported_telemetry, sentinel_telemetry,
                              exported_label, sentinel_label):
        self.calls.append(("setup_claimant", vm))
        self._paths = {exported_telemetry: "exported",
                       sentinel_telemetry: "sentinel"}
        # the local seat the marker always sees (no injected input on it) + the
        # per-stream seat (where the claimant's inject lands, iff deliver).
        seat_name = "seat0" if self.flags["wrong_seat"] else STREAM_SEAT
        exported_seats = [_seat("seat-local", 1)]
        if self.flags["deliver"]:
            exported_seats.append(
                _seat(seat_name, 2, button_press=1, pointer_motion=1))
        self._tel["exported"] = _marker_tel("exported", exported_seats)
        sentinel_seats = [_seat("seat-local", 1)]
        if self.flags["leak_sentinel"]:
            sentinel_seats.append(_seat(STREAM_SEAT, 2, button_press=1))
        self._tel["sentinel"] = _marker_tel("sentinel", sentinel_seats)
        status = {"pid": 999, "bound": 1,
                  "claim_real": int(self.flags["claim_real"]),
                  "already_claimed": int(self.flags["already_claimed"]),
                  "invalid_token": int(self.flags["invalid_token"]),
                  "go_seen": 1, "inject_sent": int(self.flags["inject_sent"]),
                  "inject_x": width // 2, "inject_y": height // 2}
        return {"status": status, "rdp_port": self.rdp_port}

    def read_claimant_status(self, vm, path):
        return {}

    def read_telemetry(self, vm, path):
        which = self._paths.get(path)
        if which == "sentinel" and self.flags["dead_sentinel"]:
            return {}                          # sentinel never wrote telemetry
        if which == "exported" and self.flags["dead_exported"]:
            return {}
        return dict(self._tel.get(which, {})) if which else {}


def _run(be, tmp_path):
    return run_direct_claimant_slice(
        be, Topology.default(), generation=7, bundle_dir=tmp_path / "b")


class TestDirectClaimant:
    def test_happy_path_passes(self, tmp_path):
        be = MockClaimantBackend()
        res = _run(be, tmp_path)
        assert res.passed
        assert res.exported_press_delta > 0 and res.sentinel_press_delta == 0
        assert res.seat_identity_ok
        assert res.pressed_seat_name == STREAM_SEAT
        assert res.expected_seat_name == STREAM_SEAT
        assert res.claim_real and res.already_claimed and res.invalid_token
        assert res.inject_sent and res.sentinel_alive
        # NOT a remote-monitor claim: no decoded-remote captures in the bundle.
        assert not res.bundle.manifest.captures

    def test_no_delivery_fails(self, tmp_path):
        # the claimed inject never reached the marker → no press → fail.
        be = MockClaimantBackend(deliver=False)
        res = _run(be, tmp_path)
        assert not res.passed and res.exported_press_delta == 0
        assert not res.seat_identity_ok

    def test_wrong_seat_fails(self, tmp_path):
        # a press landed, but NOT on the per-stream seat — the strongest fence
        # (event must travel through the stream handle, not an ambient seat).
        be = MockClaimantBackend(wrong_seat=True)
        res = _run(be, tmp_path)
        assert not res.passed
        assert res.exported_press_delta > 0 and not res.seat_identity_ok
        assert res.pressed_seat_name == "seat0"

    def test_leak_to_sentinel_fails(self, tmp_path):
        # confinement bug: the injected press also reached the local sentinel.
        be = MockClaimantBackend(leak_sentinel=True)
        res = _run(be, tmp_path)
        assert not res.passed and res.sentinel_press_delta > 0

    def test_dead_sentinel_fails_closed(self, tmp_path):
        # a sentinel that never wrote telemetry must NOT satisfy the zero for free.
        be = MockClaimantBackend(dead_sentinel=True)
        res = _run(be, tmp_path)
        assert not res.passed
        assert res.sentinel_press_delta == 0 and not res.sentinel_alive

    def test_dead_exported_fails_closed(self, tmp_path):
        # the marker never ran → a "press 0" would be vacuous → fail closed.
        be = MockClaimantBackend(dead_exported=True)
        res = _run(be, tmp_path)
        assert not res.passed and not res.exported_alive

    def test_claim_real_false_fails(self, tmp_path):
        be = MockClaimantBackend(claim_real=False)
        res = _run(be, tmp_path)
        assert not res.passed and not res.claim_real

    def test_inject_not_sent_fails(self, tmp_path):
        be = MockClaimantBackend(inject_sent=False)
        res = _run(be, tmp_path)
        assert not res.passed and not res.inject_sent

    def test_missing_already_claimed_negative_fails(self, tmp_path):
        # the one-shot-consumption contract did not hold → fail.
        be = MockClaimantBackend(already_claimed=False)
        res = _run(be, tmp_path)
        assert not res.passed and not res.already_claimed

    def test_missing_invalid_token_negative_fails(self, tmp_path):
        # a bogus token was NOT rejected → the secret gate is broken → fail.
        be = MockClaimantBackend(invalid_token=False)
        res = _run(be, tmp_path)
        assert not res.passed and not res.invalid_token

    def test_no_rdp_port_fails_seat_identity(self, tmp_path):
        # without a known rdp_port we cannot name the expected per-stream seat, so
        # the seat-identity fence cannot pass (no vacuous identity match).
        be = MockClaimantBackend(rdp_port=0)
        res = _run(be, tmp_path)
        assert not res.passed and not res.seat_identity_ok
        assert res.expected_seat_name == ""

    def test_cleanup_runs(self, tmp_path):
        be = MockClaimantBackend()
        _run(be, tmp_path)
        kinds = [c[0] for c in be.calls]
        assert kinds.count("destroy") == 1 and "setup_claimant" in kinds
