"""Tests for the pdf_export plugin."""

import pytest
from PyQt6.QtWidgets import QWidget
from qterminator.plugins.pdf_export import (
    PdfExportPlugin,
    _buffer_text,
    _print_document,
    _wrap_html,
)


class FakeInnerTerm(QWidget):
    def __init__(self):
        super().__init__()
        self.resize(120, 80)
        self._sel_text = "hello \x1b[31mworld\x1b[0m"

    def setSelectionStart(self, x, y):
        pass

    def setSelectionEnd(self, x, y):
        pass

    def selectedText(self):
        return self._sel_text

    def clearSelection(self):
        pass

    def historyLinesCount(self):
        return 0

    def screenColumnsCount(self):
        return 80

    def screenLinesCount(self):
        return 24


class FakeTerminal(QWidget):
    def __init__(self, title="term"):
        super().__init__()
        self.resize(200, 120)
        self._term = FakeInnerTerm()
        self._term.setParent(self)
        self._title = title

    def title(self):
        return self._title

    def selected_text(self):
        return ""


@pytest.fixture
def plugin():
    return PdfExportPlugin()


@pytest.fixture
def fake_terminal(qtbot):
    t = FakeTerminal()
    qtbot.addWidget(t)
    return t


def test_plugin_metadata(plugin):
    assert plugin.name == "pdf_export"
    assert plugin.version == "1.0"
    assert "menu_provider" in plugin.capabilities


def test_menu_items(plugin, fake_terminal):
    items = plugin.get_menu_items(fake_terminal)
    labels = [label for label, _ in items]
    assert "Export to PDF (visible)..." in labels
    assert "Export to PDF (full buffer)..." in labels
    assert len(items) == 2
    for _, cb in items:
        assert callable(cb)


def test_buffer_text_uses_selection_api(fake_terminal):
    text = _buffer_text(fake_terminal)
    assert "hello" in text


def test_print_document_creates_pdf(tmp_path):
    out = tmp_path / "out.pdf"
    html = _wrap_html("<p>hello world</p>", title="test")
    ok = _print_document(html, str(out))
    assert ok
    assert out.exists()
    size = out.stat().st_size
    assert size > 100  # a real PDF header + content is well over 100 bytes
    # Minimal PDF sanity: starts with %PDF-.
    with open(out, "rb") as f:
        assert f.read(5) == b"%PDF-"


def test_export_buffer_writes_pdf(plugin, fake_terminal, tmp_path, monkeypatch):
    out = tmp_path / "buf.pdf"
    monkeypatch.setattr(
        "qterminator.plugins.pdf_export.QFileDialog.getSaveFileName",
        lambda *a, **kw: (str(out), "PDF Files (*.pdf)"),
    )
    plugin._make_export_buffer(fake_terminal)()
    assert out.exists()
    assert out.stat().st_size > 100


def test_export_visible_writes_pdf(plugin, fake_terminal, tmp_path, monkeypatch):
    out = tmp_path / "vis.pdf"
    monkeypatch.setattr(
        "qterminator.plugins.pdf_export.QFileDialog.getSaveFileName",
        lambda *a, **kw: (str(out), "PDF Files (*.pdf)"),
    )
    plugin._make_export_visible(fake_terminal)()
    assert out.exists()
    assert out.stat().st_size > 100


def test_export_cancel_writes_nothing(plugin, fake_terminal, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "qterminator.plugins.pdf_export.QFileDialog.getSaveFileName",
        lambda *a, **kw: ("", ""),
    )
    plugin._make_export_visible(fake_terminal)()
    plugin._make_export_buffer(fake_terminal)()
    assert list(tmp_path.iterdir()) == []
