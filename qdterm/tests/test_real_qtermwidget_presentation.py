"""Real-binding presentation lane: fractional fonts and the scheme fallback.

Plan 04 leftovers: ``setPointSizeF`` was only proven against the in-process
fake, and the missing-scheme fallback was untested. These tests run only
where the real QTermWidget SIP binding is installed (a qdistro VM, after
fresh-vm-bootstrap step 4a); with the conftest fake they SKIP, because the
fake would echo any font or scheme back and prove nothing.

The SIP has no colour-scheme getter, so the scheme oracle is the rendered
background pixel: ``Linux`` paints black, ``BlackOnWhite`` paints white.
Run on a VM as admin in the Wayland session, e.g. with
``PYTHONPATH=<qdterm>:<sdk/presentation> QT_QPA_PLATFORM=wayland python3 -m
pytest tests/test_real_qtermwidget_presentation.py``.
"""

from __future__ import annotations

import pytest

QTermWidget = pytest.importorskip(
    "QTermWidget", reason="real QTermWidget SIP binding not installed"
)

_widget_cls = getattr(QTermWidget, "QTermWidget", QTermWidget)
if getattr(QTermWidget, "_QTERMINATOR_FAKE", False) or getattr(
    _widget_cls, "_QTERMINATOR_FAKE", False
):
    pytest.skip(
        "in-process QTermWidget fake is loaded (real SIP binding absent); "
        "the presentation lane cannot run against the fake",
        allow_module_level=True,
    )

from dataclasses import replace

import qterminator.config as config_mod
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication
from qdistro_presentation.model import example_snapshot, with_generation
from qdistro_presentation.paths import ENV_OVERRIDE
from qdistro_presentation.publish import write_snapshot
from qterminator import terminal_style
from qterminator.config import Config
from qterminator.theme import (
    attach_presentation,
    current_controller,
    reset_controller_for_tests,
)
from qterminator.window import MainWindow

DARK_SCHEME = "Linux"
LIGHT_SCHEME = "BlackOnWhite"


@pytest.fixture(autouse=True)
def fresh_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp_path / "config.toml"))
    Config._instance = None
    reset_controller_for_tests()
    yield
    reset_controller_for_tests()
    Config._instance = None


def _fixed_snapshot(family: str, scale: float):
    snap = example_snapshot()
    snap = replace(snap, mode="dark", fonts=replace(snap.fonts, fixed_family=family, fixed_scale=scale))
    return with_generation(snap)


def _background_luma(term) -> float:
    """Luma of an empty area of the rendered terminal (bottom, centre)."""
    img = term.grab().toImage()
    c = img.pixelColor(img.width() // 2, img.height() * 7 // 8)
    return 0.2126 * c.red() + 0.7152 * c.green() + 0.0722 * c.blue()


@pytest.mark.parametrize("size", [9.75, 11.5, 13.2])
def test_real_binding_keeps_fractional_point_size(qtbot, size):
    w = QTermWidget.QTermWidget(0)
    qtbot.addWidget(w)
    font = QFont("DejaVu Sans Mono")
    font.setPointSizeF(size)
    w.setTerminalFont(font)
    got = w.getTerminalFont()
    assert got.pointSizeF() == pytest.approx(size)
    assert got.family() == "DejaVu Sans Mono"


def test_real_desktop_font_follows_snapshot_and_keeps_zoom(qtbot, tmp_path, monkeypatch):
    write_snapshot(str(tmp_path), _fixed_snapshot("DejaVu Sans Mono", 1.0), require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    attach_presentation(QApplication.instance(), Config())
    win = MainWindow()
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    term = win._active_terminal
    qtbot.waitUntil(lambda: int(term.term.getShellPID() or 0) > 0, timeout=5000)
    pid = term.term.getShellPID()
    assert term.term.getTerminalFont().family() == "DejaVu Sans Mono"
    term.zoom_in()
    term.zoom_in()
    assert term.term.getTerminalFont().pointSizeF() == pytest.approx(13.0)

    snap = _fixed_snapshot("Liberation Mono", 1.2)
    write_snapshot(str(tmp_path), snap, require_unwritable_dirs=False)
    ctrl = current_controller()
    qtbot.waitUntil(lambda: ctrl.state.generation == snap.generation, timeout=5000)
    qtbot.waitUntil(lambda: term.term.getTerminalFont().family() == "Liberation Mono", timeout=5000)
    # 11 x 1.2 = 13.2 pt base + the two zoom steps, fractional on the real widget.
    assert term.term.getTerminalFont().pointSizeF() == pytest.approx(15.2)
    assert term.term.getShellPID() == pid  # restyled, not respawned
    win._zoom_normal()
    assert term.term.getTerminalFont().pointSizeF() == pytest.approx(13.2)
    win.close()


def test_real_binding_resets_unknown_scheme_so_the_guard_is_needed(qtbot):
    """The raw widget does NOT ignore an unknown name: it repaints with its
    default (white) scheme. qterminator therefore checks availability first."""
    w = QTermWidget.QTermWidget(0)
    qtbot.addWidget(w)
    w.resize(400, 300)
    w.show()
    qtbot.waitExposed(w)
    w.setColorScheme(DARK_SCHEME)
    qtbot.waitUntil(lambda: _background_luma(w) < 60, timeout=5000)
    w.setColorScheme("NoSuchScheme")
    qtbot.waitUntil(lambda: _background_luma(w) > 200, timeout=5000)


def test_real_missing_scheme_keeps_last_valid_scheme(qtbot, monkeypatch, caplog):
    available = set(_widget_cls.availableColorSchemes())
    assert {DARK_SCHEME, LIGHT_SCHEME} <= available
    assert "NoSuchScheme" not in available
    monkeypatch.setattr(terminal_style, "effective_appearance_mode", lambda: "dark")
    cfg = Config()
    cfg.set("general", "dark_color_scheme", DARK_SCHEME)
    cfg.set_profile(
        "opted",
        {
            "font_family": "DejaVu Sans Mono",
            "font_size": 11,
            "font_source": "local",
            "color_scheme": DARK_SCHEME,
            "color_source": "appearance-mode",
            "scrollback_lines": 1000,
        },
    )
    win = MainWindow()
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    term = win._active_terminal
    term.apply_profile("opted")
    term.apply_inherited_presentation()
    assert term._applied_scheme == DARK_SCHEME
    qtbot.waitUntil(lambda: _background_luma(term.term) < 60, timeout=5000)

    cfg.set("general", "dark_color_scheme", "NoSuchScheme")
    with caplog.at_level("WARNING"):
        term.apply_inherited_presentation()
    assert "NoSuchScheme" in caplog.text
    assert term._applied_scheme == DARK_SCHEME
    qtbot.wait(300)
    assert _background_luma(term.term) < 60  # still the dark scheme

    # The oracle can see a real change: a valid light scheme repaints white.
    cfg.set("general", "dark_color_scheme", LIGHT_SCHEME)
    term.apply_inherited_presentation()
    assert term._applied_scheme == LIGHT_SCHEME
    qtbot.waitUntil(lambda: _background_luma(term.term) > 200, timeout=5000)
    win.close()
