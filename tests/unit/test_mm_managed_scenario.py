"""Dry-run tests for the managed-toplevel gate (scenario-2, codex impl-8/impl-9).

A ``MockManagedBackend`` simulates the two VMs WITHOUT libvirt/FreeRDP, but —
unlike a pure stub — its ``launch_viewer`` runs the **real**
:class:`multimachine.viewer.RemoteViewer` state machine connected to the **real**
:class:`ControlServer` over a loopback socket, with a fake decoder process. So the
orchestration, the control side-channel byte path, the Announce→connected
transition, the stale-generation live rejection, and the Disconnect blanking are
all exercised end-to-end in memory. The decoded capture is synthesized by the
marker reference renderer (same as the existing ``run_viewer_slice`` mock).

The live gate (QciVMBackend) runs the identical orchestration against real VMs.
"""
from __future__ import annotations

import socket
import threading
from pathlib import Path

import numpy as np

from multimachine.bridge import ViewStreamApproved
from multimachine.harness import marker as M
from multimachine.harness.evidence import CaptureClass
from multimachine.harness.scenario import run_managed_toplevel_slice
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


class MockManagedBackend:
    """Two simulated VMs. The VM-B viewer is the real RemoteViewer over loopback."""

    def __init__(self, *, width=1280, height=800, capture_gen=7, capture_out=1,
                 source_alive=True, slot_releases=True):
        self.width, self.height = width, height
        self.capture_gen, self.capture_out = capture_gen, capture_out
        self._source_alive = source_alive
        self._slot_releases = slot_releases
        self._approval_n = 0
        self.calls: list[tuple] = []
        self._viewers: dict[str, RemoteViewer] = {}
        self._status: dict[str, dict] = {}
        self._threads: dict[str, tuple] = {}

    # ---- base VMBackend --------------------------------------------------
    def spin(self, name): self.calls.append(("spin", name)); return name

    def exec(self, vm, argv):
        self.calls.append(("exec", vm, tuple(argv)))
        return ""

    def _mint_approval(self):
        self._approval_n += 1
        return ViewStreamApproved("weston.pipewire-0", 43210 + self._approval_n,
                                  "/tmp/c.pem", f"otp{self._approval_n}")

    def subscribe_view_stream(self, vm, handle):
        self.calls.append(("subscribe", vm, handle))
        return self._mint_approval()

    def screenshot(self, vm, screen, dest):
        lay = M.compute_layout(self.width, self.height)
        pay = M.MarkerPayload(self.capture_out, self.capture_gen, 5, 0, 0,
                              self.width, self.height, 100)
        _write_ppm(Path(dest), M.render_rgb(lay, pay, scale=1.0))
        self.calls.append(("screenshot", vm, screen))
        return Path(dest)

    def apply_netem(self, vm, dev, prof): self.calls.append(("netem+", vm, dev, prof))
    def clear_netem(self, vm, dev): self.calls.append(("netem-", vm, dev))
    def destroy(self, vm): self.calls.append(("destroy", vm))

    # ---- ManagedVMBackend extensions ------------------------------------
    def launch_viewer(self, vm, *, control_host, control_port, rdp_host, rdp_port,
                      generation, otp, size, status_file):
        self.calls.append(("launch_viewer", vm, control_host, control_port))
        v = RemoteViewer(generation, lambda ann: _FakeProc(), source_machine="vm-a")
        self._viewers[vm] = v
        self._status[vm] = v.status()
        sock = socket.create_connection((control_host, control_port), timeout=5)
        sock.settimeout(None)          # blocking reads; stop_viewer wakes via shutdown

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

    def viewer_status(self, vm):
        return dict(self._status.get(vm, {}))

    def stop_viewer(self, vm):
        self.calls.append(("stop_viewer", vm))
        th = self._threads.pop(vm, None)
        if th is not None:
            t, sock = th
            try:
                sock.shutdown(socket.SHUT_RDWR)    # wake the blocked reader thread
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
            t.join(timeout=2)

    def resubscribe(self, vm):
        self.calls.append(("resubscribe", vm))
        if not self._slot_releases:
            return None
        return self._mint_approval()

    def source_alive(self, vm):
        return self._source_alive


class TestManagedSliceOrchestration:
    def test_happy_path_managed_gate_passes(self, tmp_path):
        be = MockManagedBackend(capture_gen=7, capture_out=1)
        res = run_managed_toplevel_slice(
            be, Topology.default(), generation=7, bundle_dir=tmp_path / "b",
            netem_profile="wifi-good", viewer_control_host="127.0.0.1",
            viewer_rdp_host="127.0.0.1")
        assert res.passed, res.oracle.summary()
        # the managed toplevel was reported connected with a window.
        assert res.managed_status["status"] == "connected"
        assert res.managed_status["windows"][0]["remote"] is True
        # step 9: source survived + fresh slot.
        assert res.source_alive_after_close and res.fresh_approval
        # step 10: viewer blanked, stale msg rejected live, source survived.
        assert res.disconnect_status["status"] == "disconnected"
        assert res.stale_rejected_live and res.source_alive_after_disconnect
        # honest bundle: a passing oracle on a VM_B_HOST decoded capture.
        res.bundle.assert_remote_proof()
        assert any(c.capture_class == CaptureClass.VM_B_HOST.value
                   for c in res.bundle.manifest.captures)

    def test_stale_capture_fails_gate(self, tmp_path):
        be = MockManagedBackend(capture_gen=6)            # old generation
        res = run_managed_toplevel_slice(
            be, Topology.default(), generation=7, bundle_dir=tmp_path / "b",
            viewer_control_host="127.0.0.1", viewer_rdp_host="127.0.0.1")
        assert not res.passed and res.oracle.stale_generation

    def test_source_death_fails_gate(self, tmp_path):
        be = MockManagedBackend(capture_gen=7, source_alive=False)
        res = run_managed_toplevel_slice(
            be, Topology.default(), generation=7, bundle_dir=tmp_path / "b",
            viewer_control_host="127.0.0.1", viewer_rdp_host="127.0.0.1")
        assert not res.passed and not res.source_alive_after_close

    def test_slot_not_released_fails_gate(self, tmp_path):
        # a second subscribe that does NOT yield a fresh stream is a valid failure
        # (the close did not release the slot).
        be = MockManagedBackend(capture_gen=7, slot_releases=False)
        res = run_managed_toplevel_slice(
            be, Topology.default(), generation=7, bundle_dir=tmp_path / "b",
            viewer_control_host="127.0.0.1", viewer_rdp_host="127.0.0.1")
        assert not res.passed and not res.fresh_approval

    def test_cleanup_runs_and_vms_destroyed(self, tmp_path):
        be = MockManagedBackend(capture_gen=7)
        run_managed_toplevel_slice(
            be, Topology.default(), generation=7, bundle_dir=tmp_path / "b",
            viewer_control_host="127.0.0.1", viewer_rdp_host="127.0.0.1")
        kinds = [c[0] for c in be.calls]
        assert kinds.count("destroy") == 2
        assert "netem+" in kinds and "netem-" in kinds
        assert "launch_viewer" in kinds and "resubscribe" in kinds
