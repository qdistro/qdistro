"""Importing the package must stay stdlib-only."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_package_import_does_not_load_qt_dbus_or_home():
    script = r"""
import sys
assert "PyQt6" not in sys.modules
assert "dbus" not in sys.modules
import qdistro_presentation
assert "PyQt6" not in sys.modules
assert "dbus" not in sys.modules
assert "qdistro_presentation.qt" not in sys.modules
print("ok", qdistro_presentation.SCHEMA_VERSION)
"""
    env = os.environ.copy()
    env["HOME"] = "/nonexistent-qdistro-presentation-home"
    env.pop("QDISTRO_PRESENTATION_FILE", None)
    env["PYTHONSAFEPATH"] = "1"
    env["PYTHONPATH"] = str(ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd="/",
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok 1" in proc.stdout
