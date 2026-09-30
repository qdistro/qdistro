"""Shared presentation attach for qterminator chrome."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtGui import QPalette
from qdistro_presentation.model import example_snapshot
from qdistro_presentation.paths import ENV_OVERRIDE
from qdistro_presentation.publish import write_snapshot
from qterminator.theme import attach_presentation, reset_controller_for_tests


def _config(theme_mode: str = "system", appearance: dict | None = None):
    appearance = appearance or {}

    def get(*keys, default=None):
        if keys[:2] == ("general", "theme_mode"):
            return theme_mode
        if keys == ("appearance",):
            return appearance
        return default

    return SimpleNamespace(get=get)


@pytest.fixture(autouse=True)
def _reset_presentation(qapp):
    reset_controller_for_tests()
    qapp.setPalette(QPalette())
    qapp.setStyleSheet("")
    yield
    reset_controller_for_tests()


def test_attach_presentation_follows_snapshot(qapp, tmp_path, monkeypatch):
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    resolved = attach_presentation(qapp, _config("system"))
    assert resolved == example_snapshot().mode
    assert qapp.palette().color(QPalette.ColorRole.Window).name() == example_snapshot().colors.mSurface
    assert qapp.palette().color(QPalette.ColorRole.HighlightedText).name() == (
        example_snapshot().colors.mOnPrimary
    )


def test_attach_presentation_native_ignores_snapshot(qapp, tmp_path, monkeypatch):
    native = qapp.palette().color(QPalette.ColorRole.Window).getRgb()
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    resolved = attach_presentation(qapp, _config("native"))
    assert resolved == "native"
    assert qapp.palette().color(QPalette.ColorRole.Window).getRgb() == native


def test_missing_snapshot_falls_back(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "missing.json"))
    resolved = attach_presentation(qapp, _config("system"))
    assert resolved in ("dark", "light")
