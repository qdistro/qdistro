"""Tests for the vision-agent wrapper (secondary reviewer).

No real agent is invoked: a stub QCI_AGENT_CMD echoes a verdict so we test the
contract (prompt built, verdict parsed, recorded as a *secondary* note, graceful
UNAVAILABLE when unset).
"""
from __future__ import annotations

from multimachine.harness import agent as A
from multimachine.harness.evidence import CaptureClass, EvidenceBundle


def _bundle(tmp_path):
    b = EvidenceBundle.create(tmp_path / "b", scenario="span", step="t0",
                              generation=3)
    img = tmp_path / "a.ppm"
    img.write_bytes(b"\x00")
    b.add_capture(img, CaptureClass.VM_B_HOST, role="VM-B monitor")
    return b


class TestVisionWrapper:
    def test_unavailable_without_agent_cmd(self, tmp_path, monkeypatch):
        monkeypatch.delenv("QCI_AGENT_CMD", raising=False)
        b = _bundle(tmp_path)
        obs = A.review(b, "one continuous window")
        assert obs.verdict == "UNAVAILABLE"
        assert obs.secondary is True
        assert any("UNAVAILABLE" in n for n in b.manifest.notes)

    def test_parses_verdict_from_stub_agent(self, tmp_path):
        b = _bundle(tmp_path)
        # stub: a command that prints PASS then a rationale.
        stub = "printf 'PASS\\nlooks continuous\\n'; cat {prompt} >/dev/null"
        obs = A.review(b, "one continuous window", agent_cmd=stub)
        assert obs.verdict == "PASS"
        assert obs.secondary is True
        assert (b.root / "vision-review.txt").exists()
        assert any("secondary" in n for n in b.manifest.notes)

    def test_verdict_scanned_when_not_first_line(self, tmp_path):
        b = _bundle(tmp_path)
        stub = "printf 'thinking...\\nverdict: FAIL because corruption\\n'"
        obs = A.review(b, "x", agent_cmd=stub)
        assert obs.verdict == "FAIL"

    def test_prompt_mentions_secondary_and_captures(self, tmp_path):
        b = _bundle(tmp_path)
        prompt = A.build_prompt(b, "one continuous window")
        assert "SECONDARY" in prompt.upper()
        assert "VM_B_HOST".lower() in prompt or "vm_b_host" in prompt
        assert "span" in prompt
