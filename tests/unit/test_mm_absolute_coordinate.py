"""Dry-run tests for the absolute-pixel coordinate calibration gate (A2, codex
impl-21).

A ``MockAbsCoordBackend`` models the TWO phases without VMs:
- calibration phase: a kiosk probe receives `k = T_apparatus(p)` (default 2.0x scale,
  zero offset, no cross term);
- product phase: the source marker receives `m = T_product(T_apparatus(p))`, i.e.
  `prod_scale * k + prod_offset` (default identity, so honestly `m == k`).

The headline A2 property is that a PRODUCT-side uniform-scale bug (`prod_scale != 1`)
— which the old faithful-linear-up-to-uniform-scale gate would LAUNDER as apparatus
scale — now FAILS, because the apparatus `k` was measured independently and the gate
asserts `m ≈ k` per point. Calibration-sanity and fail-closed modes are pinned too.
The live gate (QciVMBackend) runs the identical orchestration end-to-end.
"""
from __future__ import annotations

import socket
import threading
from pathlib import Path

import numpy as np

from multimachine.bridge import ViewStreamApproved
from multimachine.harness import marker as M
from multimachine.harness.scenario import run_absolute_coordinate_slice
from multimachine.harness.topology import Topology
from multimachine.sidechannel import decode
from multimachine.viewer import RemoteViewer, ViewerStatus

CALIB_TEL = "/run/mm-b/calib-probe.json"
SRC_TEL = "/run/mm-a/exported.json"


def _write_ppm(path: Path, arr: np.ndarray) -> None:
    h, w, _ = arr.shape
    with open(path, "wb") as f:
        f.write(f"P6\n{w} {h}\n255\n".encode())
        f.write(arr.astype(np.uint8).tobytes())


class _FakeProc:
    def poll(self): return None
    def terminate(self): pass


def _seat_tel(label, motion, last_x, last_y, has_pointer=1):
    seat = {"name": 1, "seat_name": "seat0", "has_pointer": has_pointer,
            "has_keyboard": 1, "pointer_enter": 1, "pointer_motion": motion,
            "button_press": 0, "keyboard_enter": 0, "key_press": 0,
            "last_x": last_x, "last_y": last_y}
    return {"label": label, "output_id": 1, "seats_seen": 1, "seats": [seat],
            "totals": {"pointer_enter": 1, "pointer_motion": motion,
                       "button_press": 0, "keyboard_enter": 0, "key_press": 0}}


class MockAbsCoordBackend:
    def __init__(self, *, capture_gen=7, app_scale=2.0, app_cross=0.0,
                 prod_scale=1.0, prod_offset=0.0, no_probe_seat=False,
                 no_source_seat=False, repeat_drift=0, width=1280, height=800):
        self.capture_gen = capture_gen
        self.app_scale, self.app_cross = app_scale, app_cross
        self.prod_scale, self.prod_offset = prod_scale, prod_offset
        self.no_probe_seat, self.no_source_seat = no_probe_seat, no_source_seat
        self.repeat_drift = repeat_drift
        self.width, self.height = width, height
        self.calls: list[tuple] = []
        self._probe_up = False
        self._motion = {CALIB_TEL: 0, SRC_TEL: 0}
        self._last = {CALIB_TEL: (0, 0), SRC_TEL: (0, 0)}
        self._inject_n = 0
        self._calib_seen: set = set()
        self._status: dict[str, dict] = {}
        self._threads: dict[str, tuple] = {}

    # ---- base / managed ----
    def spin(self, name): self.calls.append(("spin", name)); return name
    def apply_netem(self, vm, dev, prof): pass
    def clear_netem(self, vm, dev): pass
    def destroy(self, vm): self.calls.append(("destroy", vm))

    def _apparatus(self, px, py):
        kx = self.app_scale * px + self.app_cross * py
        ky = self.app_scale * py
        return (round(kx), round(ky))

    def inject_input(self, vm, *, x=None, y=None, absolute=False):
        self.calls.append(("inject", vm, x, y, absolute))
        self._inject_n += 1
        kx, ky = self._apparatus(int(x), int(y))
        # model apparatus drift on a REPEATED calibration point (the repeated-centre
        # negative control injects pts[0] a second time): the second landing differs.
        if self._probe_up:
            pt = (int(x), int(y))
            if self.repeat_drift and pt in self._calib_seen:
                kx += self.repeat_drift
            self._calib_seen.add(pt)
        # calibration probe sees the apparatus output directly.
        self._motion[CALIB_TEL] += 1
        self._last[CALIB_TEL] = (kx, ky)
        # source sees the product transform applied to the apparatus output.
        mx = round(self.prod_scale * kx + self.prod_offset)
        my = round(self.prod_scale * ky + self.prod_offset)
        self._motion[SRC_TEL] += 1
        self._last[SRC_TEL] = (mx, my)
        return (int(x), int(y))

    def read_telemetry(self, vm, path):
        if path == CALIB_TEL:
            if not self._probe_up or self.no_probe_seat:
                return {}
            mx, my = self._last[CALIB_TEL]
            return _seat_tel("calib", self._motion[CALIB_TEL], mx, my)
        if path == SRC_TEL:
            if self.no_source_seat:
                return _seat_tel("exported", 0, 0, 0, has_pointer=1)  # no motion
            mx, my = self._last[SRC_TEL]
            return _seat_tel("exported", self._motion[SRC_TEL], mx, my)
        return {}

    def setup_calibration_probe(self, vm, *, generation, telemetry=CALIB_TEL,
                                label="calib"):
        self.calls.append(("setup_calib", vm))
        self._probe_up = True
        return telemetry

    def stop_calibration_probe(self, vm):
        self.calls.append(("stop_calib", vm))
        self._probe_up = False

    def setup_confinement_source(self, vm, *, generation, width, height,
                                 exported_telemetry, sentinel_telemetry,
                                 exported_label, sentinel_label, allow_input=1):
        self.calls.append(("setup_source", vm))
        return ViewStreamApproved("pw-0", 43210, "/c.pem", "otp")

    def capture(self, vm, screen, dest):
        lay = M.compute_layout(self.width, self.height)
        pay = M.MarkerPayload(1, self.capture_gen, 5, 0, 0, self.width,
                              self.height, 100)
        _write_ppm(Path(dest), M.render_rgb(lay, pay, scale=1.0))
        return Path(dest)

    def await_decode(self, vm, timeout=25): return True

    def launch_viewer(self, vm, *, control_host, control_port, rdp_host, rdp_port,
                      generation, otp, size, status_file):
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
            _t, sock = th
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass


def _run(be, tmp_path, **kw):
    # control_port=0 → an EPHEMERAL OS-assigned port per test (the slice binds
    # control.port for the viewer), so back-to-back tests can't collide on a fixed
    # port before the prior listener is released. The live gate keeps 5556 (hostfwd).
    return run_absolute_coordinate_slice(
        be, Topology.default(), generation=7, bundle_dir=tmp_path / "b",
        control_port=0,
        viewer_control_host="127.0.0.1", viewer_rdp_host="127.0.0.1", **kw)


class TestAbsoluteCoordinate:
    def test_honest_identity_passes(self, tmp_path):
        # apparatus 2.0x, product identity -> m == k -> absolute fidelity.
        be = MockAbsCoordBackend(capture_gen=7)
        res = _run(be, tmp_path)
        assert res.passed, (res.calib_ok, res.product_ok, res.product_max_err,
                            res.calib_scale_x, res.calib_residual_max)
        assert res.calib_ok and res.product_ok
        assert abs(res.calib_scale_x - 2.0) < 0.05
        assert res.product_max_err <= 1.0
        res.bundle.assert_remote_proof()

    def test_laundered_product_scale_fails(self, tmp_path):
        # THE A2 HEADLINE: a product-side uniform scale (1.5x) that the old gate
        # would launder as apparatus scale now FAILS — m deviates from measured k.
        be = MockAbsCoordBackend(capture_gen=7, prod_scale=1.5)
        res = _run(be, tmp_path)
        assert not res.passed and not res.product_ok
        assert res.calib_ok                      # apparatus calibrated fine
        assert res.product_max_err > res.product_max_err * 0 + 4  # well over tol

    def test_product_offset_fails(self, tmp_path):
        be = MockAbsCoordBackend(capture_gen=7, prod_offset=12)
        res = _run(be, tmp_path)
        assert not res.passed and not res.product_ok
        assert res.product_max_err > 4

    def test_apparatus_cross_term_fails_calibration(self, tmp_path):
        # a non-axis-aligned apparatus (cross term) must FAIL the calibration sanity.
        be = MockAbsCoordBackend(capture_gen=7, app_cross=0.3)
        res = _run(be, tmp_path)
        assert not res.passed and not res.calib_ok

    def test_apparatus_scale_out_of_range_fails_calibration(self, tmp_path):
        be = MockAbsCoordBackend(capture_gen=7, app_scale=5.0)
        res = _run(be, tmp_path)
        assert not res.passed and not res.calib_ok

    def test_no_probe_seat_fails_closed(self, tmp_path):
        be = MockAbsCoordBackend(capture_gen=7, no_probe_seat=True)
        res = _run(be, tmp_path)
        assert not res.passed and not res.calib_ok

    def test_no_source_seat_fails_closed(self, tmp_path):
        be = MockAbsCoordBackend(capture_gen=7, no_source_seat=True)
        res = _run(be, tmp_path)
        assert not res.passed and not res.product_ok

    def test_repeat_center_drift_fails_calibration(self, tmp_path):
        # the repeated-centre control catches apparatus drift / stale telemetry.
        be = MockAbsCoordBackend(capture_gen=7, repeat_drift=10)
        res = _run(be, tmp_path)
        assert not res.passed and not res.calib_ok
        assert res.calib_repeat_dev >= 10

    def test_stale_capture_fails(self, tmp_path):
        be = MockAbsCoordBackend(capture_gen=6)        # wrong generation
        res = _run(be, tmp_path)
        assert not res.passed and res.oracle.stale_generation

    def test_phase_order_and_cleanup(self, tmp_path):
        be = MockAbsCoordBackend(capture_gen=7)
        _run(be, tmp_path)
        kinds = [c[0] for c in be.calls]
        # calibration probe set up and torn down BEFORE the source/viewer come up.
        assert kinds.index("setup_calib") < kinds.index("stop_calib")
        assert kinds.index("stop_calib") < kinds.index("setup_source")
        assert kinds.count("destroy") == 2
