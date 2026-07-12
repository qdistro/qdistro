"""Vision-agent wrapper over an evidence bundle (secondary reviewer).

09 / silos.md: "screenshots are evidence, not the strongest oracle." The
deterministic pixel oracle (``oracle.py``) is the pass/fail gate; this wrapper
runs the configured vision agent (``QCI_AGENT_CMD``, the haiku vision model) as
a *holistic secondary* over the same evidence bundle and records its observation
as a note. It can confirm "looks like one continuous window across the two
displays, correct per-machine tint, no corruption", but it never overrides the
oracle.

The ``QCI_AGENT_CMD`` contract matches qdistro CI's ``run_agent_command``: the
string contains ``{prompt}`` (substituted with a prompt-file path) or is invoked
with the prompt as its sole argument. The agent is expected to leave a verdict
word (PASS/FAIL/ERROR/SKIP) somewhere in its output.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .evidence import EvidenceBundle

_VERDICTS = ("PASS", "FAIL", "ERROR", "SKIP")


@dataclass
class VisionObservation:
    verdict: str            # PASS/FAIL/ERROR/SKIP/UNAVAILABLE
    raw: str                # full agent output (for the bundle)
    secondary: bool = True  # always — never the strong oracle


def build_prompt(bundle: EvidenceBundle, expectation: str) -> str:
    """Compose the secondary-review prompt for a bundle."""
    m = bundle.manifest
    caps = "\n".join(
        f"  - {c.path}  [{c.capture_class}]  {c.role}".rstrip()
        for c in m.captures)
    return (
        "# Multi-machine display — secondary visual review\n\n"
        "You are a SECONDARY reviewer. A deterministic pixel oracle has already\n"
        "decided pass/fail; your job is only a holistic sanity check over the\n"
        "captures and to flag anything the oracle might miss (corruption,\n"
        "obviously wrong layout, wrong per-machine tint). Do NOT override the\n"
        "oracle.\n\n"
        f"Scenario: {m.scenario}  step: {m.step}  generation: {m.generation}\n"
        f"Topology: {m.topology.vms} netem={m.topology.netem_profile}\n\n"
        f"Expectation:\n{expectation}\n\n"
        f"Captures (bundle-relative to {bundle.root}):\n{caps}\n\n"
        "Reply with exactly one of PASS/FAIL/ERROR/SKIP on its own line, then a\n"
        "one-paragraph rationale.\n")


def review(bundle: EvidenceBundle, expectation: str,
           agent_cmd: str | None = None, timeout: int = 180) -> VisionObservation:
    """Run the vision agent over the bundle; record the observation as a note.

    Returns ``UNAVAILABLE`` (not a failure) when ``QCI_AGENT_CMD`` is unset, so a
    harness run without the agent still produces the bundle + oracle verdict.
    """
    agent_cmd = agent_cmd or os.environ.get("QCI_AGENT_CMD", "")
    prompt = build_prompt(bundle, expectation)
    if not agent_cmd:
        obs = VisionObservation("UNAVAILABLE", "QCI_AGENT_CMD unset")
        bundle.manifest.notes.append("vision-agent: UNAVAILABLE (QCI_AGENT_CMD unset)")
        return obs

    prompt_file = bundle.root / "vision-prompt.txt"
    prompt_file.write_text(prompt)
    try:
        if "{prompt}" in agent_cmd:
            cmd = agent_cmd.replace("{prompt}", str(prompt_file))
            proc = subprocess.run(["bash", "-lc", cmd], capture_output=True,
                                  text=True, timeout=timeout)
        else:
            proc = subprocess.run([*agent_cmd.split(), str(prompt_file)],
                                  capture_output=True, text=True, timeout=timeout)
        out = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        obs = VisionObservation("ERROR", "vision agent timed out")
        bundle.manifest.notes.append("vision-agent: ERROR (timeout)")
        return obs

    verdict = "ERROR"
    for line in out.splitlines():
        tok = line.strip().upper()
        if tok in _VERDICTS:
            verdict = tok
            break
    else:
        m = re.search(r"\b(PASS|FAIL|ERROR|SKIP)\b", out.upper())
        if m:
            verdict = m.group(1)
    (bundle.root / "vision-review.txt").write_text(out)
    bundle.manifest.notes.append(f"vision-agent (secondary): {verdict}")
    return VisionObservation(verdict, out)
