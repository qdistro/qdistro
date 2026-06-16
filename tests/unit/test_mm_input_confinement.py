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
from multimachine.harness.scenario import run_input_confinement_slice
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


def _tel(button_press=0, key_press=0):
    return {"totals": {"button_press": button_press, "key_press": key_press,
                       "pointer_enter": 0, "keyboard_enter": 0}}


class MockInputBackend:
    def __init__(self, *, capture_gen=7, capture_out=1, leaky=False,
                 no_deliver=False, width=1280, height=800):
        self.width, self.height = width, height
        self.capture_gen, self.capture_out = capture_gen, capture_out
        self.leaky, self.no_deliver = leaky, no_deliver
        self.calls: list[tuple] = []
        self._tel = {"exported": _tel(), "sentinel": _tel()}
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
    def source_alive(self, vm): return True

    # ---- input-confinement backend --------------------------------------
    def setup_confinement_source(self, vm, *, generation, width, height,
                                 exported_telemetry, sentinel_telemetry,
                                 exported_label, sentinel_label):
        self._paths = {exported_telemetry: "exported",
                       sentinel_telemetry: "sentinel"}
        self.calls.append(("setup_confinement", vm))
        return ViewStreamApproved("pw-0", 43210, "/c.pem", "otp")

    def launch_sentinel(self, vm, *, generation, sentinel_telemetry, sentinel_label):
        self.calls.append(("launch_sentinel", vm))

    def read_telemetry(self, vm, path):
        which = self._paths.get(path)
        return dict(self._tel.get(which, {})) if which else {}

    def inject_input(self, vm):
        self.calls.append(("inject_input", vm))
        if not self.no_deliver:
            self._tel["exported"] = _tel(button_press=1, key_press=1)
        if self.leaky:                       # a confinement bug: leaks to sentinel
            self._tel["sentinel"] = _tel(button_press=1, key_press=1)


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

    def test_cleanup_runs(self, tmp_path):
        be = MockInputBackend(capture_gen=7)
        run_input_confinement_slice(
            be, Topology.default(), generation=7, bundle_dir=tmp_path / "b",
            viewer_control_host="127.0.0.1", viewer_rdp_host="127.0.0.1")
        kinds = [c[0] for c in be.calls]
        assert kinds.count("destroy") == 2 and "inject_input" in kinds
        assert "setup_confinement" in kinds
