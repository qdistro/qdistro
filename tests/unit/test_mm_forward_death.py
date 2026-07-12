"""Dry-run tests for the forward-death watch gate (item 5, codex impl-26).

``MockForwardDeathBackend`` models two concurrent input-capable exports (marker-A
+ marker-B), the qdwin-bystander subscriber log (HANDLE/RDP_PORT approval blocks +
``view_stream torn_down`` lines), and the per-forward pids. ``kill_forward`` models
qdwin's pidfd death-watch: the killed forward's stream gets a
``torn_down("forward exited")`` to the subscriber, the pid is reaped with no zombie,
and the source app + the OTHER stream are untouched. Negative knobs model the ways
the watch could be WRONG (zombie left behind, the kill tore down BOTH streams, the
source app died with the transport, the other forward got reaped, the slot didn't
free). The live gate runs the identical flow on real VMs.

Uses ``control_port=0`` (ephemeral) — the shared host flakes on fixed-5556
contention; only the LIVE gate keeps 5556 for the hostfwd.
"""
from __future__ import annotations

import socket
import threading
from pathlib import Path

import numpy as np

from multimachine.bridge import ViewStreamApproved
from multimachine.harness import marker as M
from multimachine.harness.evidence import CaptureClass
from multimachine.harness.scenario import run_forward_death_slice
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


# subscriber approval block as qdwin-bystander prints it (HANDLE then RDP_PORT).
def _approval_block(handle: int, node: str, port: int) -> str:
    return (f"qdwin-bystander: view_stream approved handle={handle}\n"
            f"HANDLE={handle}\nPIPEWIRE_NODE_NAME={node}\nRDP_PORT={port}\n"
            f"RDP_CERT_PATH=/c.pem\nRDP_PASSWORD=otp{handle}\n")


class MockForwardDeathBackend:
    PORT_A = 5555
    PORT_B = 5560
    HANDLE_A = 10
    HANDLE_B = 11
    PID_A = 101
    PID_B = 102

    def __init__(self, *, capture_gen=7, capture_out=1, width=1280, height=800,
                 leave_zombie=False, kill_tears_both=False, marker_a_dies=False,
                 kill_tears_forward_b=False, slot_not_freed=False,
                 qdwin_silent=False):
        self.width, self.height = width, height
        self.capture_gen, self.capture_out = capture_gen, capture_out
        self.leave_zombie = leave_zombie          # killed forward left as a zombie
        self.kill_tears_both = kill_tears_both    # death-watch wrongly tore down B too
        self.marker_a_dies = marker_a_dies        # source app died WITH the transport
        self.kill_tears_forward_b = kill_tears_forward_b  # B's forward also reaped
        self.slot_not_freed = slot_not_freed      # qdwin can't mint a new stream after
        self.qdwin_silent = qdwin_silent          # journal shows no teardown
        self.calls: list[tuple] = []
        self._bystander = ""
        self._journal = "qdwin: spawned qdistro-forward (pidfd death-watch armed)\n"
        self._pids = {self.PORT_A: self.PID_A, self.PORT_B: self.PID_B}
        self._reaped: set[int] = set()
        self._marker = {"mm-marker": True, "mm-marker2": True}
        self._status: dict[str, dict] = {}
        self._threads: dict[str, tuple] = {}
        self._port = {}

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
        self._port["a"] = self.PORT_A
        self._bystander += _approval_block(self.HANDLE_A, "pw-a", self.PORT_A)
        return ViewStreamApproved("pw-a", self.PORT_A, "/c.pem", "otpA")

    def setup_second_export(self, vm, *, generation, width, height, output_id,
                            telemetry, label, relay_port, allow_input=1):
        self.calls.append(("setup_b", output_id, relay_port, allow_input))
        self._port["b"] = relay_port
        self._bystander += _approval_block(self.HANDLE_B, "pw-b", relay_port)
        return ViewStreamApproved("pw-b", relay_port, "/c.pem", "otpB")

    # ---- forward-death watch probes -------------------------------------
    def forward_pids(self, vm):
        self.calls.append(("forward_pids",))
        return dict(self._pids)

    def kill_forward(self, vm, pid):
        self.calls.append(("kill_forward", pid))
        # qdwin's pidfd watch fires: stream-A torn_down to the subscriber.
        self._bystander += (
            f'qdwin-bystander: view_stream torn_down handle={self.HANDLE_A} '
            f'reason="forward exited"\n')
        if not self.qdwin_silent:
            self._journal += (
                f"qdwin: qdistro-forward pid={pid} exited; tearing down "
                f"view_stream rdp_port={self.PORT_A} (forward exited)\n")
        # the killed forward leaves the table; weston reaps it (no zombie) unless
        # we model a leak.
        self._pids.pop(self.PORT_A, None)
        if not self.leave_zombie:
            self._reaped.add(pid)
        if self.marker_a_dies:
            self._marker["mm-marker"] = False
        if self.kill_tears_both:
            self._bystander += (
                f'qdwin-bystander: view_stream torn_down handle={self.HANDLE_B} '
                f'reason="forward exited"\n')
        if self.kill_tears_forward_b:
            self._pids.pop(self.PORT_B, None)

    def pid_reaped(self, vm, pid):
        return pid in self._reaped

    def bystander_log(self, vm):
        return self._bystander

    def qdwin_journal(self, vm, tail=80):
        return self._journal

    def marker_unit_alive(self, vm, unit="mm-marker"):
        return self._marker.get(unit, False)

    def resubscribe(self, vm):
        self.calls.append(("resubscribe",))
        if self.slot_not_freed:
            return None
        # restarting the singleton bystander wipes its log (real behavior).
        self._bystander = _approval_block(12, "pw-fresh", 5599)
        return ViewStreamApproved("pw-fresh", 5599, "/c.pem", "otpFresh")

    # ---- viewer (real RemoteViewer over loopback) -----------------------
    def launch_viewer(self, vm, *, control_host, control_port, rdp_host, rdp_port,
                      generation, otp, size, status_file):
        self.calls.append(("launch_viewer", rdp_port))
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


class TestForwardDeathWatch:
    def _run(self, be, tmp_path, **kw):
        return run_forward_death_slice(
            be, Topology.default(), generation=7, bundle_dir=tmp_path / "b",
            control_port=0, viewer_control_host="127.0.0.1",
            viewer_rdp_host="127.0.0.1", **kw)

    def test_forward_death_passes(self, tmp_path):
        be = MockForwardDeathBackend(capture_gen=7, capture_out=1)
        res = self._run(be, tmp_path)
        assert res.passed, (res.torn_down_a, res.torn_down_b_absent,
                            res.forward_a_reaped, res.forward_b_alive,
                            res.marker_a_alive, res.marker_b_alive,
                            res.qdwin_detected, res.slot_freed)
        assert res.torn_down_a and res.torn_down_b_absent
        assert res.forward_a_reaped and res.forward_b_alive
        assert res.marker_a_alive and res.marker_b_alive
        assert res.qdwin_detected and res.slot_freed
        assert res.handle_a != res.handle_b
        res.bundle.assert_remote_proof()
        assert any(c.capture_class == CaptureClass.VM_B_HOST.value
                   for c in res.bundle.manifest.captures)

    def test_zombie_left_fails(self, tmp_path):
        # killed forward left as a zombie (qdwin waited / weston didn't reap) → FAIL.
        be = MockForwardDeathBackend(leave_zombie=True)
        res = self._run(be, tmp_path)
        assert not res.passed and not res.forward_a_reaped

    def test_kill_tears_both_streams_fails(self, tmp_path):
        # death-watch wrongly tore down stream-B too → per-stream isolation broken.
        be = MockForwardDeathBackend(kill_tears_both=True)
        res = self._run(be, tmp_path)
        assert not res.passed and not res.torn_down_b_absent

    def test_source_app_dies_with_transport_fails(self, tmp_path):
        # transport death must NOT be app death (process truth) → FAIL if it is.
        be = MockForwardDeathBackend(marker_a_dies=True)
        res = self._run(be, tmp_path)
        assert not res.passed and not res.marker_a_alive

    def test_other_forward_reaped_fails(self, tmp_path):
        # killing forward-A must not disturb forward-B.
        be = MockForwardDeathBackend(kill_tears_forward_b=True)
        res = self._run(be, tmp_path)
        assert not res.passed and not res.forward_b_alive

    def test_slot_not_freed_fails(self, tmp_path):
        # qdwin can't mint a new stream after the failure → teardown poisoned qdwin.
        be = MockForwardDeathBackend(slot_not_freed=True)
        res = self._run(be, tmp_path)
        assert not res.passed and not res.slot_freed

    def test_qdwin_journal_silent_fails(self, tmp_path):
        # no compositor-side teardown evidence → fail-closed.
        be = MockForwardDeathBackend(qdwin_silent=True)
        res = self._run(be, tmp_path)
        assert not res.passed and not res.qdwin_detected

    def test_stale_capture_fails(self, tmp_path):
        be = MockForwardDeathBackend(capture_gen=6)
        res = self._run(be, tmp_path)
        assert not res.passed and res.oracle.stale_generation

    def test_cleanup_runs(self, tmp_path):
        be = MockForwardDeathBackend(capture_gen=7)
        self._run(be, tmp_path)
        kinds = [c[0] for c in be.calls]
        assert kinds.count("destroy") == 2
        assert "setup_a" in kinds and "setup_b" in kinds
        assert "kill_forward" in kinds and "resubscribe" in kinds
