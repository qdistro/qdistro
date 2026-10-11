"""The VM screenshot describer uses the sanctioned visual Codex driver."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.ui import runner


@pytest.mark.parametrize("with_image", [False, True])
def test_ui_codex_driver_pins_luna_and_keeps_rollout(monkeypatch, with_image):
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append((cmd, kwargs))
        out = Path(cmd[cmd.index("--output-last-message") + 1])
        out.write_text("VERDICT: PASS\n")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    image = Path("/tmp/qdistro-ui-driver.png") if with_image else None
    assert runner._run_codex("describe this frame", image) == "VERDICT: PASS"

    cmd, kwargs = seen[0]
    assert cmd[:2] == ["codex", "exec"]
    assert cmd[cmd.index("--model") + 1] == "gpt-5.6-luna"
    assert cmd[cmd.index("-c") + 1] == "model_reasoning_effort=medium"
    assert "--ephemeral" not in cmd
    assert (f"--image={image}" in cmd) == with_image
    assert cmd[-1] == "describe this frame"
    assert kwargs["timeout"] == 180
