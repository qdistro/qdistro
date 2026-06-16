"""Dry-run tests for the step-8 input-confinement gate (scenario-3, codex impl-10).

A ``MockInputBackend`` simulates the two VMs WITHOUT libvirt/FreeRDP/ydotool: the
managed viewer comes up (real RemoteViewer over a real ControlServer, like the
managed-scenario mock) and ``inject_input`` models the shipped per-stream-seat
confinement — by default the injected presses land ONLY on the exported marker's
telemetry, never the sentinel's. ``leaky``/``no_deliver`` flags model the two
failure modes (a confinement bug, or input not arriving) so the gate's verdict is
pinned. The live gate (QciVMBackend) runs the identical orchestration end-to-end.
"""
from __future__ import annotations

import socket
import threading
from pathlib import Path

import numpy as np

from multimachine.bridge import ViewStreamApproved
from multimachine.harness import marker as M
from multimachine.harness.evidence import CaptureClass
from multimachine.harness.scenario import (
    run_input_confinement_slice, run_input_coordinate_fidelity_slice,
    run_input_negative_control_slice,
)
from multimachine.harness.topology import Topology
from multimachine.sidechannel import decode
from multimachine.viewer import RemoteViewer, ViewerStatus


def _write_ppm(path: Path, arr: np.ndarray) -> None:
    h, w, _ = arr.shape
    with open(path, "wb") as f:
        f.write(f"P6\n{w} {h}\n255\n".encode())
        f.write(arr.astype(np.uint8).tobytes())


class _FakeProc:
    def poll(self): return None
    def terminate(self): pass


def _tel(label="exported", seats_seen=1, button_press=0, key_press=0,
         pointer_motion=0, last_x=0, last_y=0):
    seats = [{"name": 1, "has_pointer": 1, "has_keyboard": 1,
              "pointer_enter": 1 if pointer_motion else 0,
              "pointer_motion": pointer_motion, "button_press": button_press,
              "keyboard_enter": 1, "key_press": key_press,
              "last_x": last_x, "last_y": last_y}]
    return {"label": label, "seats_seen": seats_seen, "seats": seats,
            "totals": {"button_press": button_press, "key_press": key_press,
                       "pointer_enter": 1 if pointer_motion else 0,
                       "pointer_motion": pointer_motion, "keyboard_enter": 0}}


class MockInputBackend:
    def __init__(self, *, capture_gen=7, capture_out=1, leaky=False,
                 no_deliver=False, dead_sentinel=False, permission_bypassed=False,
                 dead_exported=False, inject_raises=False, coord_offset=0,
                 swap_axes=False, mock_scale_x=1.0, mock_scale_y=1.0,
                 width=1280, height=800):
        self.width, self.height = width, height
        self.capture_gen, self.capture_out = capture_gen, capture_out
        self.leaky, self.no_deliver = leaky, no_deliver
        self.dead_sentinel = dead_sentinel
        self.permission_bypassed = permission_bypassed   # neg-ctrl: bit failed to gate
        self.dead_exported = dead_exported               # neg-ctrl: exported never ran
        self.inject_raises = inject_raises               # neg-ctrl: injection not driven
        self.coord_offset = coord_offset                 # coord-fidelity: translation bug
        self.swap_axes = swap_axes                       # coord-fidelity: cross-axis swap
        self.mock_scale_x = mock_scale_x                 # coord-fidelity: per-axis scale
        self.mock_scale_y = mock_scale_y
        self._motion = 0
        self.calls: list[tuple] = []
        self._tel = {"exported": _tel("exported"), "sentinel": _tel("sentinel")}
        self._paths: dict[str, str] = {}
        self._threads: dict[str, tuple] = {}
        self._status: dict[str, dict] = {}

    # ---- base + managed backend -----------------------------------------
    def spin(self, name): self.calls.append(("spin", name)); return name
    def apply_netem(self, vm, dev, prof): self.calls.append(("netem+", vm))
    def clear_netem(self, vm, dev): self.calls.append(("netem-", vm))
    def destroy(self, vm): self.calls.append(("destroy", vm))
    def exec(self, vm, argv): return ""
    def subscribe_view_stream(self, vm, handle):
        return ViewStreamApproved("pw-0", 43210, "/c.pem", "otp")

    def capture(self, vm, screen, dest):
        lay = M.compute_layout(self.width, self.height)
        pay = M.MarkerPayload(self.capture_out, self.capture_gen, 5, 0, 0,
                              self.width, self.height, 100)
        _write_ppm(Path(dest), M.render_rgb(lay, pay, scale=1.0))
        return Path(dest)
    screenshot = capture

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

    def resubscribe(self, vm): return None
    def source_alive(self, vm): return not self.dead_exported

    # ---- input-confinement backend --------------------------------------
    def setup_confinement_source(self, vm, *, generation, width, height,
                                 exported_telemetry, sentinel_telemetry,
                                 exported_label, sentinel_label, allow_input=1):
        self._paths = {exported_telemetry: "exported",
                       sentinel_telemetry: "sentinel"}
        self._allow_input = allow_input        # 0 = read-only negative control
        self.calls.append(("setup_confinement", vm, allow_input))
        return ViewStreamApproved("pw-0", 43210, "/c.pem", "otp")

    def launch_sentinel(self, vm, *, generation, sentinel_telemetry, sentinel_label):
        self.calls.append(("launch_sentinel", vm))

    def read_telemetry(self, vm, path):
        which = self._paths.get(path)
        if which == "sentinel" and self.dead_sentinel:
            return {}                        # sentinel never wrote telemetry
        if which == "exported" and self.dead_exported:
            return {}                        # exported marker never wrote telemetry
        return dict(self._tel.get(which, {})) if which else {}

    def inject_input(self, vm, *, x=None, y=None, absolute=False):
        self.calls.append(("inject_input", vm))
        if self.inject_raises:
            raise RuntimeError("ydotool injection failed")
        px = 0 if x is None else int(x)
        py = 0 if y is None else int(y)
        # allow_input=0 (negative control): the forward got NO inject channel, so
        # the SAME injection delivers nothing — UNLESS the permission bit failed to
        # gate it (permission_bypassed). allow_input=1 delivers as normal. The
        # injected pointer lands at the modelled end-to-end map (mock_scale·p +
        # coord_offset); pointer_motion increments per injection (coord-fidelity).
        deliver = (getattr(self, "_allow_input", 1) or self.permission_bypassed)
        if deliver and not self.no_deliver:
            self._motion += 1
            ix, iy = (py, px) if self.swap_axes else (px, py)   # swap = cross-axis bug
            self._tel["exported"] = _tel(
                "exported", button_press=1, key_press=1, pointer_motion=self._motion,
                last_x=round(self.mock_scale_x * ix) + self.coord_offset,
                last_y=round(self.mock_scale_y * iy) + self.coord_offset)
        if self.leaky:                       # a confinement bug: leaks to sentinel
            self._tel["sentinel"] = _tel("sentinel", button_press=1, key_press=1)
        return (px, py)


class TestInputConfinement:
    def test_confined_input_passes(self, tmp_path):
        be = MockInputBackend(capture_gen=7)
        res = run_input_confinement_slice(
            be, Topology.default(), generation=7, bundle_dir=tmp_path / "b",
            viewer_control_host="127.0.0.1", viewer_rdp_host="127.0.0.1")
        assert res.passed
        assert res.exported_press_delta > 0 and res.sentinel_press_delta == 0
        res.bundle.assert_remote_proof()
        assert any(c.capture_class == CaptureClass.VM_B_HOST.value
                   for c in res.bundle.manifest.captures)

    def test_leak_to_sentinel_fails(self, tmp_path):
        # a confinement bug: injected input reaches the local sentinel too.
        be = MockInputBackend(capture_gen=7, leaky=True)
        res = run_input_confinement_slice(
            be, Topology.default(), generation=7, bundle_dir=tmp_path / "b",
            viewer_control_host="127.0.0.1", viewer_rdp_host="127.0.0.1")
        assert not res.passed and res.sentinel_press_delta > 0

    def test_input_not_delivered_fails(self, tmp_path):
        # input never reaches the exported marker (path broken) → fail.
        be = MockInputBackend(capture_gen=7, no_deliver=True)
        res = run_input_confinement_slice(
            be, Topology.default(), generation=7, bundle_dir=tmp_path / "b",
            viewer_control_host="127.0.0.1", viewer_rdp_host="127.0.0.1")
        assert not res.passed and res.exported_press_delta == 0

    def test_stale_capture_fails(self, tmp_path):
        # the decoded oracle is still a gate: a wrong-generation capture fails.
        be = MockInputBackend(capture_gen=6)
        res = run_input_confinement_slice(
            be, Topology.default(), generation=7, bundle_dir=tmp_path / "b",
            viewer_control_host="127.0.0.1", viewer_rdp_host="127.0.0.1")
        assert not res.passed and res.oracle.stale_generation

    def test_dead_sentinel_fails_closed(self, tmp_path):
        # codex impl-11: a sentinel that never wrote telemetry must NOT satisfy the
        # negative-control half for free — sentinel_delta==0 but unproven → FAIL.
        be = MockInputBackend(capture_gen=7, dead_sentinel=True)
        res = run_input_confinement_slice(
            be, Topology.default(), generation=7, bundle_dir=tmp_path / "b",
            viewer_control_host="127.0.0.1", viewer_rdp_host="127.0.0.1")
        assert not res.passed and res.sentinel_press_delta == 0

    def test_cleanup_runs(self, tmp_path):
        be = MockInputBackend(capture_gen=7)
        run_input_confinement_slice(
            be, Topology.default(), generation=7, bundle_dir=tmp_path / "b",
            viewer_control_host="127.0.0.1", viewer_rdp_host="127.0.0.1")
        kinds = [c[0] for c in be.calls]
        assert kinds.count("destroy") == 2 and "inject_input" in kinds
        assert "setup_confinement" in kinds


class TestNegativeControl:
    """read-only allow_input=0 negative control (codex impl-11/13)."""

    def _run(self, be, tmp_path):
        return run_input_negative_control_slice(
            be, Topology.default(), generation=7, bundle_dir=tmp_path / "b",
            viewer_control_host="127.0.0.1", viewer_rdp_host="127.0.0.1",
            settle=0)

    def test_read_only_blocks_injection_passes(self, tmp_path):
        # allow_input=0 → the forward gets no inject channel → BOTH deltas stay 0.
        be = MockInputBackend(capture_gen=7)
        res = self._run(be, tmp_path)
        assert res.passed
        assert res.exported_press_delta == 0 and res.sentinel_press_delta == 0
        assert res.inject_attempted and res.exported_alive and res.exported_valid
        # the export really was read-only (allow_input=0 reached the source stack).
        assert ("setup_confinement", "vm-a", 0) in be.calls
        res.bundle.assert_remote_proof()

    def test_permission_bit_broken_fails(self, tmp_path):
        # if injection DID reach the exported window despite allow_input=0, the
        # permission bit failed to gate it → the negative control must FAIL.
        be = MockInputBackend(capture_gen=7, permission_bypassed=True)
        res = self._run(be, tmp_path)
        assert not res.passed and res.exported_press_delta > 0

    def test_dead_exported_fails_closed(self, tmp_path):
        # a 0 delta is vacuous if the exported marker never ran — fail closed.
        be = MockInputBackend(capture_gen=7, dead_exported=True)
        res = self._run(be, tmp_path)
        assert not res.passed
        assert not (res.exported_alive and res.exported_valid)

    def test_injection_not_attempted_fails(self, tmp_path):
        # a 0 delta is only meaningful if the SAME injection was actually driven.
        be = MockInputBackend(capture_gen=7, inject_raises=True)
        res = self._run(be, tmp_path)
        assert not res.passed and not res.inject_attempted

    def test_stale_capture_fails(self, tmp_path):
        be = MockInputBackend(capture_gen=6)             # wrong generation
        res = self._run(be, tmp_path)
        assert not res.passed and res.oracle.stale_generation


class TestCoordinateFidelity:
    """inject at TWO known viewer pixels, assert the source receives them as a
    faithful linear map — zero offset, isotropic scale (codex impl-11; session 6).
    The uniform apparatus scale is allowed; offset/skew/swap must fail."""

    def _run(self, be, tmp_path, **kw):
        return run_input_coordinate_fidelity_slice(
            be, Topology.default(), generation=7, bundle_dir=tmp_path / "b",
            viewer_control_host="127.0.0.1", viewer_rdp_host="127.0.0.1", **kw)

    def test_faithful_identity_passes(self, tmp_path):
        be = MockInputBackend(capture_gen=7)             # identity map
        res = self._run(be, tmp_path)
        assert res.passed, (res.x_scale, res.y_scale, res.offset_x, res.offset_y,
                            res.cross_x_shear, res.cross_y_shear)
        assert abs(res.offset_x) <= res.offset_tol and abs(res.offset_y) <= res.offset_tol
        assert abs(res.x_scale - res.y_scale) <= res.scale_tol
        assert abs(res.cross_x_shear) <= res.shear_tol
        assert abs(res.cross_y_shear) <= res.shear_tol and res.seats_found
        res.bundle.assert_remote_proof()

    def test_uniform_apparatus_scale_passes(self, tmp_path):
        # a UNIFORM scale (the ydotool uinput-absolute artifact) is faithful → pass.
        be = MockInputBackend(capture_gen=7, mock_scale_x=2.0, mock_scale_y=2.0)
        res = self._run(be, tmp_path)
        assert res.passed
        assert abs(res.x_scale - 2.0) < 0.05 and abs(res.offset_x) <= res.offset_tol

    def test_offset_translation_fails(self, tmp_path):
        # a coordinate OFFSET (the origin doesn't map faithfully) must FAIL.
        be = MockInputBackend(capture_gen=7, coord_offset=80)
        res = self._run(be, tmp_path)
        assert not res.passed and abs(res.offset_x) > res.offset_tol

    def test_anisotropic_scale_fails(self, tmp_path):
        # per-axis distortion (x_scale != y_scale) must FAIL.
        be = MockInputBackend(capture_gen=7, mock_scale_x=1.0, mock_scale_y=2.0)
        res = self._run(be, tmp_path)
        assert not res.passed and abs(res.x_scale - res.y_scale) > res.scale_tol

    def test_axis_swap_fails(self, tmp_path):
        # an axis SWAP (x↔y) is invisible on a single diagonal but the axis-aligned
        # triple catches it: cross shear blows up (and x/y scale degenerate).
        be = MockInputBackend(capture_gen=7, swap_axes=True)
        res = self._run(be, tmp_path)
        assert not res.passed
        assert (abs(res.cross_x_shear) > res.shear_tol
                or abs(res.cross_y_shear) > res.shear_tol
                or res.x_scale <= 0.1 or res.y_scale <= 0.1)

    def test_no_injected_seat_fails_closed(self, tmp_path):
        be = MockInputBackend(capture_gen=7, no_deliver=True)
        res = self._run(be, tmp_path)
        assert not res.passed and not res.seats_found

    def test_stale_capture_fails(self, tmp_path):
        be = MockInputBackend(capture_gen=6)
        res = self._run(be, tmp_path)
        assert not res.passed and res.oracle.stale_generation
