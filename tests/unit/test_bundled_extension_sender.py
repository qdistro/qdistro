"""Finding #14 — run the bundled-extension JS assertions.

The bundled ``browser_bridge/extension`` has no vitest harness, so the
hardening is verified by self-contained node assertion scripts. This pytest
wrapper runs them so the JS checks are part of the normal suite; it skips
when node is unavailable.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_JS_TEST_DIR = (Path(__file__).resolve().parent.parent.parent
                / "browser_bridge" / "extension" / "tests")

_JS_TESTS = [
    _JS_TEST_DIR / "background.sender.test.js",
    _JS_TEST_DIR / "manifest.permissions.test.js",
]


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node not available")
@pytest.mark.parametrize("script", _JS_TESTS, ids=lambda p: p.name)
def test_bundled_extension_js_assertions(script):
    assert script.exists(), script
    proc = subprocess.run(
        ["node", str(script)],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (
        f"node assertions failed:\nSTDOUT:\n{proc.stdout}\n"
        f"STDERR:\n{proc.stderr}")
    assert "all assertions passed" in proc.stdout
