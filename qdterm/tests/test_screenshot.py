"""Tests for the screenshot plugin."""


import pytest
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QApplication, QWidget
from qterminator.plugins.screenshot import ScreenshotPlugin


class FakeInnerTerm(QWidget):
    """Stand-in for QTermWidget's internal widget; inherits QWidget.grab."""

    def __init__(self):
        super().__init__()
        self.resize(120, 80)

    # Expose optional APIs used by the buffer grab.
    def historyLinesCount(self):
        return 0

    def screenLinesCount(self):
        return 24


class FakeTerminal(QWidget):
    def __init__(self, title="term"):
        super().__init__()
        self.resize(160, 100)
        self._term = FakeInnerTerm()
        self._term.setParent(self)
        self._title = title

    def title(self):
        return self._title

    def selected_text(self):
        return ""


@pytest.fixture
def plugin():
    return ScreenshotPlugin()


@pytest.fixture
def fake_terminal(qtbot):
    t = FakeTerminal()
    qtbot.addWidget(t)
    return t


def test_plugin_metadata(plugin):
    assert plugin.name == "screenshot"
    assert plugin.version == "1.0"
    assert "menu_provider" in plugin.capabilities


def test_menu_items_labels(plugin, fake_terminal):
    items = plugin.get_menu_items(fake_terminal)
    labels = [label for label, _ in items]
    assert "Screenshot Visible Area..." in labels
    assert "Screenshot Entire Buffer..." in labels
    assert "Screenshot to Clipboard" in labels
    assert len(items) == 3
    # All callbacks should be callable.
    for _, cb in items:
        assert callable(cb)


def test_grab_visible_returns_pixmap(plugin, fake_terminal):
    pix = plugin._grab_visible(fake_terminal)
    assert isinstance(pix, QPixmap)
    assert not pix.isNull()


def test_save_visible_writes_png(plugin, fake_terminal, tmp_path, monkeypatch):
    out = tmp_path / "shot.png"
    monkeypatch.setattr(
        "qterminator.plugins.screenshot.QFileDialog.getSaveFileName",
        lambda *a, **kw: (str(out), "PNG Images (*.png)"),
    )
    plugin._make_save_visible(fake_terminal)()
    assert out.exists()
    assert out.stat().st_size > 0


def test_save_visible_cancel_writes_nothing(
    plugin, fake_terminal, tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        "qterminator.plugins.screenshot.QFileDialog.getSaveFileName",
        lambda *a, **kw: ("", ""),
    )
    plugin._make_save_visible(fake_terminal)()
    # No files were created by the plugin in tmp_path.
    assert list(tmp_path.iterdir()) == []


def test_clipboard_action(plugin, fake_terminal):
    # Ensure a QApplication exists for clipboard access.
    assert QApplication.instance() is not None
    plugin._make_clipboard(fake_terminal)()
    pm = QApplication.clipboard().pixmap()
    assert isinstance(pm, QPixmap)
    assert not pm.isNull()


def test_save_buffer_falls_back_to_visible(
    plugin, fake_terminal, tmp_path, monkeypatch,
):
    # FakeInnerTerm has no scrollToTop/Bottom -> code path returns visible
    # and a "truncated" flag but should still save something.
    out = tmp_path / "buf.png"
    monkeypatch.setattr(
        "qterminator.plugins.screenshot.QFileDialog.getSaveFileName",
        lambda *a, **kw: (str(out), "PNG Images (*.png)"),
    )
    # Information dialog is suppressed (only shown when truncated==True and
    # history exists); with history=0 we shouldn't hit it, but guard anyway.
    monkeypatch.setattr(
        "qterminator.plugins.screenshot.QMessageBox.information",
        lambda *a, **kw: None,
    )
    plugin._make_save_buffer(fake_terminal)()
    assert out.exists()
    assert out.stat().st_size > 0
