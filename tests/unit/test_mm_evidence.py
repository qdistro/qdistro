"""Tests for the evidence-bundle format.

Pins the bundle contract (09 "build now" item 1) and, crucially, the honesty
rule: a bundle may not claim to prove the remote monitor's content from a
source-intent capture only.
"""
from __future__ import annotations

import json

import pytest

from multimachine.harness.evidence import (
    CaptureClass, EvidenceBundle, OracleRecord, Topology,
)


def _img(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"\x00\x01\x02fakeimg")
    return p


class TestEvidenceBundle:
    def test_create_write_load_roundtrip(self, tmp_path):
        b = EvidenceBundle.create(
            tmp_path / "bundle", scenario="static-spanning", step="t0",
            generation=3, topology=Topology(vms=["vm-a", "vm-b"],
                                            netem_profile="wifi-good"))
        b.add_capture(_img(tmp_path, "a.ppm"), CaptureClass.VM_A_HOST,
                      output_id=0, role="VM-A display-1")
        b.add_capture(_img(tmp_path, "b.ppm"), CaptureClass.VM_B_HOST,
                      output_id=0, role="VM-B monitor")
        b.add_oracle(OracleRecord(capture="captures/b.ppm", ok=True,
                                  generation=3, frame=10))
        b.manifest.passed = True
        path = b.write()
        assert path.exists()

        loaded = EvidenceBundle.load(tmp_path / "bundle")
        assert loaded.manifest.scenario == "static-spanning"
        assert loaded.manifest.generation == 3
        assert loaded.manifest.topology.netem_profile == "wifi-good"
        assert len(loaded.manifest.captures) == 2
        assert loaded.manifest.oracle[0].ok is True
        assert loaded.manifest.passed is True

    def test_captures_copied_into_bundle(self, tmp_path):
        b = EvidenceBundle.create(tmp_path / "bundle", scenario="s")
        b.add_capture(_img(tmp_path, "x.ppm"), CaptureClass.VM_A_GUEST)
        assert (tmp_path / "bundle" / "captures" / "x.ppm").exists()

    def test_manifest_is_valid_json(self, tmp_path):
        b = EvidenceBundle.create(tmp_path / "bundle", scenario="s")
        b.write()
        json.loads((tmp_path / "bundle" / "manifest.json").read_text())

    def test_unsupported_schema_rejected(self, tmp_path):
        b = EvidenceBundle.create(tmp_path / "bundle", scenario="s")
        b.write()
        mf = tmp_path / "bundle" / "manifest.json"
        data = json.loads(mf.read_text())
        data["schema_version"] = 999
        mf.write_text(json.dumps(data))
        with pytest.raises(ValueError, match="schema"):
            EvidenceBundle.load(tmp_path / "bundle")


class TestHonestyRule:
    def test_source_only_bundle_refused(self, tmp_path):
        b = EvidenceBundle.create(tmp_path / "bundle", scenario="span")
        b.add_capture(_img(tmp_path, "a.ppm"), CaptureClass.VM_A_GUEST)
        b.add_capture(_img(tmp_path, "src.ppm"), CaptureClass.VM_A_RDP_SOURCE)
        with pytest.raises(ValueError, match="decoded-remote"):
            b.assert_remote_proof()

    def test_decoded_remote_bundle_accepted(self, tmp_path):
        b = EvidenceBundle.create(tmp_path / "bundle", scenario="span")
        b.add_capture(_img(tmp_path, "a.ppm"), CaptureClass.VM_A_GUEST)
        b.add_capture(_img(tmp_path, "b.ppm"), CaptureClass.VM_B_HOST)
        b.assert_remote_proof()  # must not raise
