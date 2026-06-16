"""Dry-run tests for the 2nd-exported-view (A→B) input isolation gate (impl-15).

``MockSecondViewBackend`` models two concurrent input-capable exports (marker-A on
output 1, marker-B on output 2) and runs the REAL viewer over the REAL ControlServer
each phase. ``inject_input`` delivers a press to whichever stream the CURRENT viewer
is bound to (tracked by rdp_port) and — by default — ONLY that stream's marker;
``leaky`` models a broken per-stream isolation that cross-delivers. So the
two-phase orchestration (B positive-control, then A isolation) + the verdict are
exercised end-to-end in memory. The live gate runs the identical flow on real VMs.
"""
from __future__ import annotations

import socket
import threading
from pathlib import Path

import numpy as np

from multimachine.bridge import ViewStreamApproved
from multimachine.harness import marker as M
from multimachine.harness.evidence import CaptureClass
from multimachine.harness.scenario import run_second_view_isolation_slice
from multimachine.harness.topology import Topology
from multimachine.sidechannel import decode
from multimachine.viewer import RemoteViewer, ViewerStatus


def _write_ppm(path: Path, arr: np.ndarray) -> None:
    h, w, _ = arr.shape
    with open(path, "wb") as f:
        f.write(f"P6\n{w} {h}\n255\n".encode())
        f.write(arr.astype(np.uint8).tobytes())


def _tel(label, output_id, presses):
    return {"label": label, "output_id": output_id, "generation": 7,
            "seats_seen": 1,
            "seats": [{"name": output_id, "has_pointer": 1, "pointer_motion": 1,
                       "button_press": presses, "key_press": 0, "last_x": 0,
                       "last_y": 0}],
            "totals": {"button_press": presses, "key_press": 0}}


class _FakeProc:
    def poll(self): return None
    def terminate(self): pass


class MockSecondViewBackend:
    def __init__(self, *, capture_gen=7, capture_out=1, leaky=False,
                 b_seat_dead=False, b_dies_after_iso=False, marker2_dead=False,
                 width=1280, height=800):
        self.width, self.height = width, height
        self.capture_gen, self.capture_out = capture_gen, capture_out
        self.leaky = leaky                    # broken isolation: A inject leaks to B
        self.b_seat_dead = b_seat_dead        # marker-B seat can't deliver (pos ctrl fails)
        self.b_dies_after_iso = b_dies_after_iso  # B seat dies after phase A (re-proof fails)
        self.marker2_dead = marker2_dead      # mm-marker2/relay2 not live in phase A
        self._injects = 0
        self.calls: list[tuple] = []
        self._press = {"a": 0, "b": 0}
        self._out = {"a": 1, "b": 2}
        self._port = {}                       # "a"/"b" -> rdp_port
        self._current = None                  # which stream the live viewer decodes
        self._status: dict[str, dict] = {}
        self._threads: dict[str, tuple] = {}

    # ---- base ------------------------------------------------------------
    def spin(self, name): self.calls.append(("spin", name)); return name
    def apply_netem(self, vm, dev, prof): self.calls.append(("netem+", vm))
    def clear_netem(self, vm, dev): self.calls.append(("netem-", vm))
    def destroy(self, vm): self.calls.append(("destroy", vm))
    def exec(self, vm, argv): return ""
    def await_decode(self, vm, timeout=25): return True
    def source_alive(self, vm): return True

    def capture(self, vm, screen, dest):
        lay = M.compute_layout(self.width, self.height)
        pay = M.MarkerPayload(self.capture_out, self.capture_gen, 5, 0, 0,
                              self.width, self.height, 100)
        _write_ppm(Path(dest), M.render_rgb(lay, pay, scale=1.0))
        return Path(dest)
    screenshot = capture

    # ---- two exports -----------------------------------------------------
    def setup_confinement_source(self, vm, *, generation, width, height,
                                 exported_telemetry, sentinel_telemetry,
                                 exported_label, sentinel_label, allow_input=1):
        self.calls.append(("setup_a", allow_input))
        self._tel_a = exported_telemetry
        self._port["a"] = 5555
        return ViewStreamApproved("pw-a", 5555, "/c.pem", "otpA")

    def setup_second_export(self, vm, *, generation, width, height, output_id,
                            telemetry, label, relay_port, allow_input=1):
        self.calls.append(("setup_b", output_id, relay_port, allow_input))
        self._tel_b = telemetry
        self._out["b"] = output_id
        self._port["b"] = relay_port
        return ViewStreamApproved("pw-b", relay_port, "/c.pem", "otpB")

    def read_telemetry(self, vm, path):
        if path == getattr(self, "_tel_a", None):
            return _tel("exported-a", self._out["a"], self._press["a"])
        if path == getattr(self, "_tel_b", None):
            return _tel("exported-b", self._out["b"], self._press["b"])
        return {}

    def inject_input(self, vm, *, x=None, y=None, absolute=False):
        self.calls.append(("inject_input", self._current))
        self._injects += 1
        if self._current == "b" and self.b_seat_dead:
            return (0, 0)                     # marker-B's seat never deliverable
        # b_dies_after_iso: B's seat works in phase B1 (inject #1) but is dead by the
        # phase-B2 re-proof (inject #3) — models a seat that didn't survive isolation.
        if self._current == "b" and self.b_dies_after_iso and self._injects >= 3:
            return (0, 0)
        if self._current is not None:
            self._press[self._current] += 1
            other = "b" if self._current == "a" else "a"
            if self.leaky:                    # broken per-stream isolation
                self._press[other] += 1
        return (0, 0)

    def marker2_alive(self, vm):
        return not self.marker2_dead

    # ---- viewer (real RemoteViewer over loopback) -----------------------
    def launch_viewer(self, vm, *, control_host, control_port, rdp_host, rdp_port,
                      generation, otp, size, status_file):
        self._current = "a" if rdp_port == self._port.get("a") else "b"
        self.calls.append(("launch_viewer", self._current, rdp_port))
        self.stop_viewer(vm)
        v = RemoteViewer(generation, lambda ann: _FakeProc(), source_machine="vm-a")
        self._status[vm] = v.status()
        sock = socket.create_connection((control_host, control_port), timeout=5)
        sock.settimeout(None)

        def loop():
            f = sock.makefile("r")
            for line in f:
                line = line.strip()
                if not line:
                    continue
                v.on_message(decode(line))
                self._status[vm] = v.status()
                if v.status_value in (ViewerStatus.DISCONNECTED,
                                      ViewerStatus.CAPACITY_EXCEEDED):
                    break
        t = threading.Thread(target=loop, daemon=True)
        t.start()
        self._threads[vm] = (t, sock)

    def viewer_status(self, vm): return dict(self._status.get(vm, {}))

    def stop_viewer(self, vm):
        th = self._threads.pop(vm, None)
        if th is not None:
            t, sock = th
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
            t.join(timeout=2)


class TestSecondViewIsolation:
    def _run(self, be, tmp_path, **kw):
        return run_second_view_isolation_slice(
            be, Topology.default(), generation=7, bundle_dir=tmp_path / "b",
            viewer_control_host="127.0.0.1", viewer_rdp_host="127.0.0.1", **kw)

    def test_isolated_two_exports_passes(self, tmp_path):
        be = MockSecondViewBackend(capture_gen=7, capture_out=1)
        res = self._run(be, tmp_path)
        assert res.passed, (res.marker_a_delta, res.marker_b_positive_delta,
                            res.marker_b_isolation_delta, res.marker_b_reproof_delta,
                            res.distinct_views)
        assert res.marker_a_delta > 0
        assert res.marker_b_positive_delta > 0      # B's seat proven deliverable (B1)
        assert res.marker_b_isolation_delta == 0    # A inject did not leak to B
        assert res.marker_b_reproof_delta > 0       # B's seat SURVIVED isolation (B2)
        assert res.marker_b_alive and res.marker_b_valid and res.distinct_views
        res.bundle.assert_remote_proof()
        assert any(c.capture_class == CaptureClass.VM_B_HOST.value
                   for c in res.bundle.manifest.captures)

    def test_cross_stream_leak_fails(self, tmp_path):
        # broken per-stream isolation: injecting at viewer-A also presses marker-B.
        be = MockSecondViewBackend(capture_gen=7, leaky=True)
        res = self._run(be, tmp_path)
        assert not res.passed and res.marker_b_isolation_delta > 0

    def test_dead_b_seat_fails_positive_control(self, tmp_path):
        # marker-B's seat can't deliver → the 0 isolation delta would be vacuous →
        # the positive control fails the gate (fail-closed).
        be = MockSecondViewBackend(capture_gen=7, b_seat_dead=True)
        res = self._run(be, tmp_path)
        assert not res.passed and res.marker_b_positive_delta == 0

    def test_b_seat_dies_after_isolation_fails_reproof(self, tmp_path):
        # B's seat delivered in phase B1 but died during/after isolation → the
        # phase-B2 re-proof catches it, so the 0 isolation delta is NOT trusted.
        be = MockSecondViewBackend(capture_gen=7, b_dies_after_iso=True)
        res = self._run(be, tmp_path)
        assert not res.passed
        assert res.marker_b_positive_delta > 0 and res.marker_b_reproof_delta == 0

    def test_marker2_not_live_fails(self, tmp_path):
        # mm-marker2/mm-relay2 not live through phase A → liveness witness fails.
        be = MockSecondViewBackend(capture_gen=7, marker2_dead=True)
        res = self._run(be, tmp_path)
        assert not res.passed and not res.marker_b_alive

    def test_stale_capture_fails(self, tmp_path):
        be = MockSecondViewBackend(capture_gen=6)
        res = self._run(be, tmp_path)
        assert not res.passed and res.oracle.stale_generation

    def test_cleanup_runs(self, tmp_path):
        be = MockSecondViewBackend(capture_gen=7)
        self._run(be, tmp_path)
        kinds = [c[0] for c in be.calls]
        assert kinds.count("destroy") == 2
        assert "setup_a" in kinds and "setup_b" in kinds
        assert kinds.count("launch_viewer") == 3     # B1, A, B2 re-proof
