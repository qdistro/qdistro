"""Polkit prompt applies a startup-only trusted snapshot.

Developer path overrides are ignored. Appearance failure must not block
the prompt, reorder focus, or write to stdout (stdout carries the
password).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
QtGui = pytest.importorskip("PyQt6.QtGui")

from PyQt6.QtGui import QPalette  # noqa: E402
from PyQt6.QtWidgets import QApplication, QDialog, QLabel, QLineEdit  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _load_prompt():
    path = _ROOT / "polkit" / "qdistro-polkit-prompt.py"
    spec = importlib.util.spec_from_file_location("qdistro_polkit_prompt", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    return app


@pytest.fixture
def prompt():
    return _load_prompt()


def test_attach_ignores_developer_override(qapp, tmp_path, prompt, monkeypatch):
    from qdistro_presentation import paths as paths_mod
    from qdistro_presentation.model import example_snapshot
    from qdistro_presentation.publish import write_snapshot
    from qdistro_presentation.qt import reset_controller_for_tests

    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv("QDISTRO_PRESENTATION_FILE", str(tmp_path / "current.json"))
    real = paths_mod.resolve_snapshot_path
    absent = str(tmp_path / "no-managed")

    def wrapped(*, role="ordinary", environ=None, managed_dir=paths_mod.MANAGED_DIR):
        return real(role=role, environ=environ, managed_dir=absent)

    monkeypatch.setattr(paths_mod, "resolve_snapshot_path", wrapped)
    native = QPalette(qapp.palette()).color(QPalette.ColorRole.Window).getRgb()
    reset_controller_for_tests()
    ctrl = prompt._attach_trusted_appearance(qapp)
    assert ctrl is not None
    assert ctrl._role == "polkit"
    assert ctrl._watch_enabled is False
    assert ctrl._watcher is None
    # Managed directory is absent, so polkit must not consume the override.
    assert qapp.palette().color(QPalette.ColorRole.Window).getRgb() == native
    ctrl.stop()
    reset_controller_for_tests()


def test_attach_failure_still_prompts(qapp, prompt):
    captured = {}

    def fake_exec(self):
        edits = self.findChildren(QLineEdit)
        assert edits
        captured["edit"] = edits[0]
        captured["focus"] = self.focusWidget()
        edits[0].setText("secret-pw")
        return QDialog.DialogCode.Accepted

    with patch.object(prompt, "_attach_trusted_appearance", side_effect=RuntimeError("boom")):
        with patch("PyQt6.QtWidgets.QDialog.exec", fake_exec):
            result = prompt._qt_prompt("org.qdistro.test", "Auth required")
    assert result == "secret-pw"
    assert captured["edit"].hasFocus() or captured["focus"] is captured["edit"]


def test_action_label_uses_secondary_text_role(qapp, prompt):
    seen = {}

    def fake_exec(self):
        labels = self.findChildren(QLabel)
        action_labels = [lab for lab in labels if lab.text().startswith("Action:")]
        assert action_labels, [lab.text() for lab in labels]
        seen["role"] = action_labels[0].foregroundRole()
        seen["sheet"] = action_labels[0].styleSheet()
        return QDialog.DialogCode.Rejected

    with patch("PyQt6.QtWidgets.QDialog.exec", fake_exec):
        result = prompt._qt_prompt("org.qdistro.test", "Auth required")
    assert result is None
    assert seen["role"] == QPalette.ColorRole.PlaceholderText
    assert "#666" not in seen["sheet"]


def test_password_stdout_is_only_the_secret(qapp, prompt, capsys):
    def fake_exec(self):
        for child in self.findChildren(QLineEdit):
            child.setText("only-this")
        return QDialog.DialogCode.Accepted

    args = type("A", (), {"mode": "pam", "action": "a", "message": "m"})()
    with patch.object(prompt, "_parse", return_value=args):
        with patch("PyQt6.QtWidgets.QDialog.exec", fake_exec):
            code = prompt.main()
    out = capsys.readouterr()
    assert code == 0
    assert out.out == "only-this\n"
    assert "presentation" not in out.out.lower()
    assert "theme" not in out.out.lower()
