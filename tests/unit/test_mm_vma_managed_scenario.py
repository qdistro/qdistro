"""Dry-run tests for the VM-A-SERVED managed-toplevel gate (codex impl-12).

Unlike the host-served gate's mock (which used the host ``ControlServer`` to
produce control bytes), this ``MockVmaBackend`` runs the **real**
:func:`multimachine.control_source.watch` + :class:`ControlSource` as the control
PRODUCER — serving the **real** :class:`multimachine.viewer.RemoteViewer` over a
loopback socket. So the in-guest Announce build, the source-owned watch decision
(no Closed on viewer detach, source-driven Closed on marker death), and the
viewer's proxy removal are all exercised end-to-end in memory. The host never
produces a control byte — exactly the honesty property the live gate asserts.

The live gate (QciVMBackend) runs the identical orchestration against real VMs,
where the producer is a VM-A ``systemd --user`` ``mm-control`` unit.
"""
from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import numpy as np

from multimachine.bridge import SourceWindowInfo, ViewStreamApproved
from multimachine.control_source import (
    VIEWER_ALIVE, VIEWER_DATA, VIEWER_EOF, ControlSource, watch,
)
from multimachine.harness import marker as M
from multimachine.harness.evidence import CaptureClass
from multimachine.harness.scenario import run_managed_toplevel_vma_slice
from multimachine.harness.topology import Topology
from multimachine.sidechannel import decode, encode
from multimachine.viewer import RemoteViewer, ViewerStatus


def _write_ppm(path: Path, arr: np.ndarray) -> None:
    h, w, _ = arr.shape
    with open(path, "wb") as f:
        f.write(f"P6\n{w} {h}\n255\n".encode())
        f.write(arr.astype(np.uint8).tobytes())


class _FakeProc:
    def poll(self): return None
    def terminate(self): pass


class MockVmaBackend:
    """Two simulated VMs. VM-A's control PRODUCER is the real ControlSource/watch
    over loopback; VM-B's viewer is the real RemoteViewer."""

    def __init__(self, *, width=1280, height=800, capture_gen=7, capture_out=1,
                 slot_releases=True, marker_dies_on_kill=True):
        self.width, self.height = width, height
        self.capture_gen, self.capture_out = capture_gen, capture_out
        self._slot_releases = slot_releases
        self._marker_dies_on_kill = marker_dies_on_kill
        self._approval_n = 0
        self.control_port = 0
        self.calls: list[tuple] = []
        # control producer state (per launch_control)
        self._marker_alive = True
        self._ctl_srv: socket.socket | None = None
        self._ctl_thread: threading.Thread | None = None
        self._ctl_sent: list[dict] = []
        self._ctl_reason = ""
        # viewer state
        self._viewers: dict[str, RemoteViewer] = {}
        self._status: dict[str, dict] = {}
        self._vthreads: dict[str, tuple] = {}

    # ---- base VMBackend --------------------------------------------------
    def spin(self, name): self.calls.append(("spin", name)); return name
    def exec(self, vm, argv): self.calls.append(("exec", vm, tuple(argv))); return ""
    def apply_netem(self, vm, dev, prof): self.calls.append(("netem+", vm))
    def clear_netem(self, vm, dev): self.calls.append(("netem-", vm))
    def destroy(self, vm): self.calls.append(("destroy", vm))

    def _mint_approval(self):
        self._approval_n += 1
        return ViewStreamApproved("weston.pipewire-0", 43210 + self._approval_n,
                                  "/tmp/c.pem", f"otp{self._approval_n}")

    def subscribe_view_stream(self, vm, handle):
        self.calls.append(("subscribe", vm, handle))
        return self._mint_approval()

    def capture(self, vm, screen, dest):
        lay = M.compute_layout(self.width, self.height)
        pay = M.MarkerPayload(self.capture_out, self.capture_gen, 5, 0, 0,
                              self.width, self.height, 100)
        _write_ppm(Path(dest), M.render_rgb(lay, pay, scale=1.0))
        return Path(dest)

    def await_decode(self, vm, timeout=25): return True

    # ---- VM-A-served control PRODUCER (real ControlSource + watch) -------
    def launch_control(self, vm, *, generation, window_id, source_machine, title,
                       app_id, req_w, req_h, marker_unit="mm-marker"):
        self.calls.append(("launch_control", vm))
        # tear down any prior producer (relaunch per export, like the real unit).
        self.stop_control(vm)
        self._marker_alive = True
        self._ctl_sent = []
        self._ctl_reason = ""
        src = SourceWindowInfo(window_id=window_id, source_machine=source_machine,
                               title=title, app_id=app_id, req_w=req_w, req_h=req_h)
        source = ControlSource.from_source(src, generation)
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.control_port = srv.getsockname()[1]
        self._ctl_srv = srv

        def serve():
            srv.settimeout(10)
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            conn.settimeout(None)

            def send(msg):
                conn.sendall((encode(msg) + "\n").encode())
                self._ctl_sent.append(json.loads(encode(msg)))

            def poll_viewer():
                import select
                r, _, _ = select.select([conn], [], [], 0.1)
                if not r:
                    return VIEWER_ALIVE
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    return VIEWER_EOF
                return VIEWER_DATA if chunk else VIEWER_EOF

            self._ctl_reason = watch(
                source, is_source_alive=lambda: self._marker_alive,
                poll_viewer=poll_viewer, send=send)
            try:
                conn.close()
            except OSError:
                pass

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        self._ctl_thread = t
        return source.meta.stream_id

    def control_log(self, vm):
        return {"sent": list(self._ctl_sent), "reason": self._ctl_reason}

    def stop_control(self, vm):
        srv, self._ctl_srv = self._ctl_srv, None
        if srv is not None:
            try:
                srv.close()
            except OSError:
                pass
        t, self._ctl_thread = self._ctl_thread, None
        if t is not None:
            t.join(timeout=2)

    def kill_marker(self, vm):
        self.calls.append(("kill_marker", vm))
        if self._marker_dies_on_kill:
            self._marker_alive = False
        # give the watcher a beat to observe + emit the source-driven Closed.
        import time
        for _ in range(50):
            if any(m.get("type") == "closed" for m in self._ctl_sent):
                break
            time.sleep(0.02)

    def source_alive(self, vm):
        return self._marker_alive

    def resubscribe(self, vm):
        self.calls.append(("resubscribe", vm))
        return self._mint_approval() if self._slot_releases else None

    # ---- VM-B viewer (real RemoteViewer over loopback) ------------------
    def launch_viewer(self, vm, *, control_host, control_port, rdp_host, rdp_port,
                      generation, otp, size, status_file):
        self.calls.append(("launch_viewer", vm, control_host, control_port))
        self.stop_viewer(vm)
        v = RemoteViewer(generation, lambda ann: _FakeProc(), source_machine="vm-a")
        self._viewers[vm] = v
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
        self._vthreads[vm] = (t, sock)

    def viewer_status(self, vm):
        return dict(self._status.get(vm, {}))

    def stop_viewer(self, vm):
        th = self._vthreads.pop(vm, None)
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


class TestVmaManagedSliceOrchestration:
    def test_happy_path_vma_gate_passes(self, tmp_path):
        be = MockVmaBackend(capture_gen=7, capture_out=1)
        res = run_managed_toplevel_vma_slice(
            be, Topology.default(), generation=7, bundle_dir=tmp_path / "b",
            netem_profile="wifi-good", viewer_control_host="127.0.0.1",
            viewer_rdp_host="127.0.0.1")
        assert res.passed, (res.oracle.summary(), res.detach_reason,
                            res.source_closed_emitted, res.viewer_proxy_removed)
        # control is VM-A-served: never host-served.
        assert res.host_served_control is False
        # the stream_id the viewer showed is the one VM-A minted + announced.
        assert res.viewer_stream_id and res.viewer_stream_id == res.announced_stream_id
        # step 9: detach, NOT source death — no Closed, source alive, slot frees.
        assert res.detach_reason == "viewer-eof"
        assert res.detach_emitted_closed is False
        assert res.source_alive_after_detach and res.fresh_approval
        # step 10: source-driven Closed removed the viewer proxy; marker now dead.
        assert res.source_closed_emitted and res.viewer_proxy_removed
        assert res.source_dead_after_close
        # honest bundle.
        res.bundle.assert_remote_proof()
        assert any(c.capture_class == CaptureClass.VM_B_HOST.value
                   for c in res.bundle.manifest.captures)

    def test_stale_capture_fails_gate(self, tmp_path):
        be = MockVmaBackend(capture_gen=6)               # old generation
        res = run_managed_toplevel_vma_slice(
            be, Topology.default(), generation=7, bundle_dir=tmp_path / "b",
            viewer_control_host="127.0.0.1", viewer_rdp_host="127.0.0.1")
        assert not res.passed and res.oracle.stale_generation

    def test_slot_not_released_fails_gate(self, tmp_path):
        be = MockVmaBackend(capture_gen=7, slot_releases=False)
        res = run_managed_toplevel_vma_slice(
            be, Topology.default(), generation=7, bundle_dir=tmp_path / "b",
            viewer_control_host="127.0.0.1", viewer_rdp_host="127.0.0.1")
        assert not res.passed and not res.fresh_approval
        # the detach half still held (no spurious Closed on the viewer kill).
        assert res.detach_reason == "viewer-eof" and not res.detach_emitted_closed

    def test_source_not_dying_fails_closed_half(self, tmp_path):
        # if killing the marker does NOT actually end the source, mm-control emits
        # no Closed and the gate fails (it must prove source-driven teardown).
        be = MockVmaBackend(capture_gen=7, marker_dies_on_kill=False)
        res = run_managed_toplevel_vma_slice(
            be, Topology.default(), generation=7, bundle_dir=tmp_path / "b",
            viewer_control_host="127.0.0.1", viewer_rdp_host="127.0.0.1")
        assert not res.passed
        assert not res.source_closed_emitted

    def test_cleanup_runs_and_vms_destroyed(self, tmp_path):
        be = MockVmaBackend(capture_gen=7)
        run_managed_toplevel_vma_slice(
            be, Topology.default(), generation=7, bundle_dir=tmp_path / "b",
            viewer_control_host="127.0.0.1", viewer_rdp_host="127.0.0.1")
        kinds = [c[0] for c in be.calls]
        assert kinds.count("destroy") == 2
        assert "launch_control" in kinds and "kill_marker" in kinds
        assert "netem+" in kinds and "netem-" in kinds
