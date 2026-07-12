"""Dry-run tests for the bounded PipeWire reconnect gate (item 6, codex impl-28).

``MockForwardReconnectBackend`` models the forward's fault-injection behavior via the
qdwin journal lines it would emit, plus forward liveness, the subscriber log, and the
marker unit. ``transient`` recovers (journal "stream recovered", forward stays alive,
no torn_down); ``persistent`` gives up (journal "reconnect budget exhausted", forward
exits, subscriber torn_down). Negative knobs model the ways each path could be WRONG.
The live gate runs the identical flow on real VMs.

Uses ``control_port=0`` (ephemeral) — the shared host flakes on fixed-5556 contention.
"""
from __future__ import annotations

import socket
import threading
from pathlib import Path

import numpy as np

from multimachine.bridge import ViewStreamApproved
from multimachine.harness import marker as M
from multimachine.harness.evidence import CaptureClass
from multimachine.harness.scenario import run_forward_reconnect_slice
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


_J_INJECT = "FAULT-INJECT: synthesizing one PipeWire stream error"
_J_START = "PipeWire stream error (gen=1) — starting bounded reconnect"
_J_RECOVERED = "PipeWire stream recovered after 1 attempt(s)"
_J_GAVEUP = "FATAL: PipeWire reconnect budget exhausted (5 attempts, 3750ms)"
_J_TEARDOWN = "qdistro-forward pid=4242 exited; tearing down view_stream rdp_port=3401 (forward exited)"


class MockForwardReconnectBackend:
    PORT = 3401
    HANDLE = 7
    PID = 4242

    def __init__(self, *, mode, capture_gen=7, capture_out=1, width=1280, height=800,
                 no_inject=False, no_recover=False, leak_torn_down=False,
                 no_giveup=False, forward_survives_giveup=False,
                 no_torn_down_on_exit=False, marker_dies=False):
        self.mode = mode
        self.width, self.height = width, height
        self.capture_gen, self.capture_out = capture_gen, capture_out
        self.calls: list[tuple] = []
        self._port = {}
        self._status: dict[str, dict] = {}
        self._threads: dict[str, tuple] = {}
        self.marker_dies = marker_dies
        # build the journal + end-state per mode and knobs.
        lines = []
        if not no_inject:
            lines.append(_J_INJECT)
            lines.append(_J_START)
        if mode == "transient":
            if not no_recover:
                lines.append(_J_RECOVERED)
            self._forward_pids = {} if no_recover else {self.PORT: self.PID}
            self._bystander = ("torn_down handle=%d reason=\"forward exited\"" % self.HANDLE
                               ) if leak_torn_down else ""
        else:  # persistent
            if not no_giveup:
                lines.append(_J_GAVEUP)
                lines.append(_J_TEARDOWN)
            self._forward_pids = ({self.PORT: self.PID}
                                  if forward_survives_giveup else {})
            self._bystander = "" if no_torn_down_on_exit else (
                "qdwin-bystander: view_stream torn_down handle=%d "
                "reason=\"forward exited\"" % self.HANDLE)
        self._journal = "\n".join(lines) + "\n"

    # ---- base ------------------------------------------------------------
    def spin(self, name): self.calls.append(("spin", name)); return name
    def apply_netem(self, vm, dev, prof): self.calls.append(("netem+", vm))
    def clear_netem(self, vm, dev): self.calls.append(("netem-", vm))
    def destroy(self, vm): self.calls.append(("destroy", vm))
    def exec(self, vm, argv): return ""
    def await_decode(self, vm, timeout=25): return True

    def capture(self, vm, screen, dest):
        lay = M.compute_layout(self.width, self.height)
        pay = M.MarkerPayload(self.capture_out, self.capture_gen, 5, 0, 0,
                              self.width, self.height, 100)
        _write_ppm(Path(dest), M.render_rgb(lay, pay, scale=1.0))
        return Path(dest)
    screenshot = capture

    def setup_confinement_source(self, vm, *, generation, width, height,
                                 exported_telemetry, sentinel_telemetry,
                                 exported_label, sentinel_label, allow_input=1,
                                 fault=""):
        self.calls.append(("setup", fault))
        self._port["a"] = self.PORT
        return ViewStreamApproved("pw-a", self.PORT, "/c.pem", "otpA")

    # ---- reconnect probes ------------------------------------------------
    def qdwin_journal(self, vm, tail=80): return self._journal
    def forward_pids(self, vm): return dict(self._forward_pids)
    def bystander_log(self, vm): return self._bystander
    def marker_unit_alive(self, vm, unit="mm-marker"): return not self.marker_dies

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
            for fn in (lambda: sock.shutdown(socket.SHUT_RDWR), sock.close):
                try:
                    fn()
                except OSError:
                    pass
            t.join(timeout=2)


class TestForwardReconnect:
    def _run(self, be, tmp_path, mode, **kw):
        return run_forward_reconnect_slice(
            be, Topology.default(), mode=mode, generation=7,
            bundle_dir=tmp_path / "b", control_port=0,
            viewer_control_host="127.0.0.1", viewer_rdp_host="127.0.0.1", **kw)

    # ---- transient (recover) --------------------------------------------
    def test_transient_recovers(self, tmp_path):
        be = MockForwardReconnectBackend(mode="transient", capture_gen=7)
        res = self._run(be, tmp_path, "transient")
        assert res.passed, (res.fault_injected, res.reconnect_started,
                            res.recovered, res.forward_alive, res.no_torn_down,
                            res.marker_alive, res.oracle.ok)
        assert res.recovered and res.forward_alive and res.no_torn_down
        res.bundle.assert_remote_proof()
        assert any(c.capture_class == CaptureClass.VM_B_HOST.value
                   for c in res.bundle.manifest.captures)

    def test_transient_forward_exit_fails(self, tmp_path):
        # forward did not recover (exited) → forward_alive False → FAIL.
        be = MockForwardReconnectBackend(mode="transient", no_recover=True)
        res = self._run(be, tmp_path, "transient")
        assert not res.passed and not res.forward_alive and not res.recovered

    def test_transient_spurious_torn_down_fails(self, tmp_path):
        # the stream should stay up; any torn_down means it didn't truly recover.
        be = MockForwardReconnectBackend(mode="transient", leak_torn_down=True)
        res = self._run(be, tmp_path, "transient")
        assert not res.passed and not res.no_torn_down

    def test_transient_stale_capture_fails(self, tmp_path):
        be = MockForwardReconnectBackend(mode="transient", capture_gen=6)
        res = self._run(be, tmp_path, "transient")
        assert not res.passed and res.oracle.stale_generation

    # ---- persistent (give up → exit → torn_down) ------------------------
    def test_persistent_gives_up_and_exits(self, tmp_path):
        be = MockForwardReconnectBackend(mode="persistent")
        res = self._run(be, tmp_path, "persistent")
        assert res.passed, (res.fault_injected, res.reconnect_started, res.gave_up,
                            res.forward_exited, res.torn_down, res.marker_alive)
        assert res.gave_up and res.forward_exited and res.torn_down
        # persistent path proves a self-exit + torn_down, not remote pixels — so
        # there is deliberately NO decoded-remote capture / assert_remote_proof here.

    def test_persistent_no_torn_down_fails(self, tmp_path):
        # forward exited but qdwin did not post torn_down → FAIL.
        be = MockForwardReconnectBackend(mode="persistent", no_torn_down_on_exit=True)
        res = self._run(be, tmp_path, "persistent")
        assert not res.passed and not res.torn_down

    def test_persistent_forward_survives_fails(self, tmp_path):
        # budget message present but forward did not actually exit → FAIL.
        be = MockForwardReconnectBackend(mode="persistent", forward_survives_giveup=True)
        res = self._run(be, tmp_path, "persistent")
        assert not res.passed and not res.forward_exited

    def test_persistent_marker_dies_fails(self, tmp_path):
        # transport give-up must NOT take the source app with it (process truth).
        be = MockForwardReconnectBackend(mode="persistent", marker_dies=True)
        res = self._run(be, tmp_path, "persistent")
        assert not res.passed and not res.marker_alive

    def test_cleanup_runs(self, tmp_path):
        be = MockForwardReconnectBackend(mode="transient")
        self._run(be, tmp_path, "transient")
        kinds = [c[0] for c in be.calls]
        assert kinds.count("destroy") == 2 and ("setup" in kinds)
