"""Finding #14 — run the bundled-extension JS sender/RNG assertions.

The bundled ``browser_bridge/extension`` has no vitest harness, so the
hardening is verified by a self-contained node assertion script
(``background.sender.test.js``). This pytest wrapper runs it so the JS
checks are part of the normal suite; it skips when node is unavailable.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_JS_TEST = (Path(__file__).resolve().parent.parent.parent
            / "browser_bridge" / "extension" / "tests"
            / "background.sender.test.js")


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node not available")
def test_bundled_extension_sender_and_rng():
    assert _JS_TEST.exists(), _JS_TEST
    proc = subprocess.run(
        ["node", str(_JS_TEST)],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (
        f"node assertions failed:\nSTDOUT:\n{proc.stdout}\n"
        f"STDERR:\n{proc.stderr}")
    assert "all assertions passed" in proc.stdout
