"""Quarantine panel meta chrome follows the UI font and shared dim color."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from PyQt6.QtGui import QFont, QPalette
from PyQt6.QtWidgets import QWidget
from qdbrowser.theme import (
    _ui_font,
    attach_presentation,
    palette_dict,
    reset_controller_for_tests,
)
from qdistro_presentation.model import example_snapshot, with_generation
from qdistro_presentation.paths import ENV_OVERRIDE
from qdistro_presentation.publish import write_snapshot


@pytest.fixture(autouse=True)
def _reset_presentation(qapp):
    reset_controller_for_tests()
    native_font = QFont(qapp.font())
    native_palette = QPalette(qapp.palette())
    native_qss = qapp.styleSheet()
    yield
    reset_controller_for_tests()
    qapp.setPalette(QPalette(native_palette))
    qapp.setStyleSheet(native_qss)
    qapp.setFont(QFont(native_font))


def _config(theme_mode: str = "system"):
    def get(*keys, default=None):
        if keys[:2] == ("general", "theme_mode"):
            return theme_mode
        if keys == ("appearance",):
            return {}
        return default

    return SimpleNamespace(get=get)


def _scaled_snapshot():
    snap = example_snapshot()
    return with_generation(replace(
        snap,
        fonts=replace(snap.fonts, ui_scale=1.2),
        generation="",
    ))


def _attach_snapshot(qapp, tmp_path, monkeypatch, snap):
    write_snapshot(str(tmp_path), snap, require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    attach_presentation(qapp, _config("system"))


def _make_store(tmp_path):
    from qdbrowser.quarantine import QuarantineStore
    return QuarantineStore(str(tmp_path / "quar"))


def _quarantined(store, name="doc.pdf", scan="clean"):
    import os

    qpath = os.path.join(store.directory, name)
    with open(qpath, "wb") as f:
        f.write(b"contents")
    return store.record(
        quarantine_path=qpath, filename=name,
        source_url=f"https://x/{name}", scan_result=scan)


def _panel_with_row(qtbot, tmp_path):
    from qdbrowser.plugins.quarantine_panel import (
        QuarantineController,
        QuarantinePanel,
    )

    host = QWidget()
    qtbot.addWidget(host)
    store = _make_store(tmp_path)
    _quarantined(store, "a.bin")
    panel = QuarantinePanel(None, QuarantineController(store), parent=host)
    assert panel._list.count() == 1
    assert panel._meta_labels
    return host, panel, store


def test_quarantine_meta_uses_ui_font_not_pixel_size(qtbot, tmp_path):
    _host, panel, store = _panel_with_row(qtbot, tmp_path)
    meta = panel._meta_labels[0]
    expected = _ui_font(relative=0.9)
    sheet = meta.styleSheet()
    assert "font-size" not in sheet
    assert "11px" not in sheet
    assert "palette(mid)" not in sheet
    assert meta.font().family() == expected.family()
    assert meta.font().pointSizeF() == pytest.approx(expected.pointSizeF())
    store.close()


def test_quarantine_panel_restyles_existing_meta_from_snapshot(
    qtbot, qapp, tmp_path, monkeypatch
):
    _host, panel, store = _panel_with_row(qtbot, tmp_path)
    meta = panel._meta_labels[0]
    old_sheet = meta.styleSheet()
    old_family = meta.font().family()
    old_size = meta.font().pointSizeF()
    old_text = meta.text()
    snap = _scaled_snapshot()
    dim = snap.colors.mOnSurfaceVariant.lower()
    assert dim not in old_sheet.lower()
    assert "font-size" not in old_sheet

    _attach_snapshot(qapp, tmp_path, monkeypatch, snap)
    assert meta.styleSheet() == old_sheet
    assert meta.font().family() == old_family
    assert meta.font().pointSizeF() == pytest.approx(old_size)
    assert meta.text() == old_text
    assert panel._meta_labels[0] is meta
    assert panel._list.count() == 1

    panel.apply_presentation_update()
    expected = _ui_font(relative=0.9)
    sheet = meta.styleSheet()
    assert dim in sheet.lower()
    assert "font-size" not in sheet
    assert "11px" not in sheet
    assert "palette(mid)" not in sheet
    assert meta.font().family() == expected.family()
    assert meta.font().pointSizeF() == pytest.approx(expected.pointSizeF())
    assert expected.pointSizeF() != pytest.approx(old_size)
    assert meta.text() == old_text
    assert panel._meta_labels[0] is meta
    assert panel._list.count() == 1
    store.close()


def test_quarantine_plugin_restyles_existing_panel(
    qtbot, qapp, tmp_path, monkeypatch
):
    from qdbrowser.plugins.quarantine_panel import QuarantinePanelPlugin

    _host, panel, store = _panel_with_row(qtbot, tmp_path)
    plugin = QuarantinePanelPlugin()
    plugin._panel = panel
    meta = panel._meta_labels[0]
    old_sheet = meta.styleSheet()
    old_text = meta.text()
    snap = _scaled_snapshot()
    dim = snap.colors.mOnSurfaceVariant.lower()

    _attach_snapshot(qapp, tmp_path, monkeypatch, snap)
    assert meta.styleSheet() == old_sheet
    assert meta.text() == old_text

    plugin.apply_presentation_update()
    assert dim in meta.styleSheet().lower()
    assert "font-size" not in meta.styleSheet()
    assert meta.text() == old_text
    assert panel._meta_labels[0] is meta
    store.close()


def test_mainwindow_presentation_update_restyles_quarantine_panel(
    qtbot, qapp, tmp_path, monkeypatch, window
):
    from qdbrowser.plugins.quarantine_panel import QuarantinePanel

    plug = window.plugins._instances["quarantine_panel"]
    panel = plug._panel
    assert isinstance(panel, QuarantinePanel)
    _quarantined(panel._controller.store, "live.bin")
    panel.refresh()
    assert panel._meta_labels
    meta = panel._meta_labels[0]
    old_sheet = meta.styleSheet()
    old_text = meta.text()
    snap = _scaled_snapshot()
    dim = snap.colors.mOnSurfaceVariant.lower()
    assert dim not in old_sheet.lower()

    _attach_snapshot(qapp, tmp_path, monkeypatch, snap)
    assert meta.styleSheet() == old_sheet
    assert meta.text() == old_text

    window.apply_presentation_update()
    assert dim in meta.styleSheet().lower()
    assert "font-size" not in meta.styleSheet()
    assert meta.text() == old_text
    assert panel._meta_labels[0] is meta
    assert palette_dict("auto")["fg_dim"].lower() == dim
