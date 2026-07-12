"""Dry-run tests for the two-VM scenario orchestrator (MM-01 viewer slice).

A MockBackend drives the full orchestration with synthetic decoded captures
(rendered by the marker reference renderer) — no libvirt, no FreeRDP. Validates
the orchestration logic, the evidence-bundle assembly + honesty rule, netem
apply/clear ordering, and the pass/fail wiring while the VM is contended.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from multimachine.bridge import ViewStreamApproved
from multimachine.harness import marker as M
from multimachine.harness.evidence import CaptureClass
from multimachine.harness.scenario import run_viewer_slice
from multimachine.harness.topology import Topology


def _write_ppm(path: Path, arr: np.ndarray) -> None:
    h, w, _ = arr.shape
    with open(path, "wb") as f:
        f.write(f"P6\n{w} {h}\n255\n".encode())
        f.write(arr.astype(np.uint8).tobytes())


class MockBackend:
    """Simulates two VMs. ``capture_gen``/``capture_out``/``source_alive`` let a
    test inject a stale/wrong/dead decoded capture."""

    def __init__(self, *, width=800, height=600, capture_gen=1, capture_out=1,
                 source_alive=True):
        self.width, self.height = width, height
        self.capture_gen, self.capture_out = capture_gen, capture_out
        self._source_alive = source_alive
        self.calls: list[tuple] = []

    def spin(self, name): self.calls.append(("spin", name)); return name

    def exec(self, vm, argv):
        self.calls.append(("exec", vm, tuple(argv)))
        if argv[:1] == ["pgrep"]:
            return "1234\n" if self._source_alive else ""
        return ""

    def subscribe_view_stream(self, vm, handle):
        self.calls.append(("subscribe", vm, handle))
        return ViewStreamApproved("weston.pipewire-3", 43210, "/tmp/c.pem", "otp")

    def screenshot(self, vm, screen, dest):
        # synthesize the DECODED-REMOTE capture the peer would show.
        lay = M.compute_layout(self.width, self.height)
        pay = M.MarkerPayload(self.capture_out, self.capture_gen, 5, 0, 0,
                              self.width, self.height, 100)
        _write_ppm(Path(dest), M.render_rgb(lay, pay, scale=1.0))
        self.calls.append(("screenshot", vm, screen))
        return Path(dest)

    def apply_netem(self, vm, dev, prof): self.calls.append(("netem+", vm, dev, prof))
    def clear_netem(self, vm, dev): self.calls.append(("netem-", vm, dev))
    def destroy(self, vm): self.calls.append(("destroy", vm))


class TestViewerSliceOrchestration:
    def test_happy_path_passes_and_builds_honest_bundle(self, tmp_path):
        be = MockBackend(capture_gen=7, capture_out=1)
        res = run_viewer_slice(be, Topology.default(), generation=7,
                               bundle_dir=tmp_path / "b", netem_profile="wifi-good",
                               marker_output_id=1)
        assert res.passed, res.oracle.summary()
        assert res.oracle.ok and res.source_alive
        # bundle is honest: a passing oracle record on a decoded-remote capture.
        res.bundle.assert_remote_proof()
        caps = res.bundle.manifest.captures
        assert any(c.capture_class == CaptureClass.VM_B_HOST.value for c in caps)
        assert res.bundle.manifest.passed is True
        # teardown blanked the proxy (detach).
        assert res.viewer.windows == {}

    def test_stale_generation_capture_fails(self, tmp_path):
        # decoded capture stamped with an old generation -> oracle rejects.
        be = MockBackend(capture_gen=6)
        res = run_viewer_slice(be, Topology.default(), generation=7,
                               bundle_dir=tmp_path / "b")
        assert not res.passed
        assert res.oracle.stale_generation

    def test_source_death_fails_the_slice(self, tmp_path):
        # the source app dying on teardown is a FAIL (detach must keep it alive).
        be = MockBackend(capture_gen=7, source_alive=False)
        res = run_viewer_slice(be, Topology.default(), generation=7,
                               bundle_dir=tmp_path / "b")
        assert not res.source_alive and not res.passed

    def test_netem_applied_and_cleared_and_vms_destroyed(self, tmp_path):
        be = MockBackend(capture_gen=7)
        run_viewer_slice(be, Topology.default(), generation=7,
                         bundle_dir=tmp_path / "b", netem_profile="wifi-bad")
        kinds = [c[0] for c in be.calls]
        assert "netem+" in kinds and "netem-" in kinds
        # netem cleared and both VMs destroyed even though we passed through.
        assert kinds.count("destroy") == 2
        assert kinds.index("netem+") < kinds.index("netem-")

    def test_cleanup_runs_even_on_failure(self, tmp_path):
        be = MockBackend(capture_gen=999, capture_out=1)  # stale -> fail
        run_viewer_slice(be, Topology.default(), generation=7,
                         bundle_dir=tmp_path / "b")
        kinds = [c[0] for c in be.calls]
        assert "netem-" in kinds and kinds.count("destroy") == 2
