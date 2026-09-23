"""Tests for the print_terminal plugin.

These tests do not actually print — they mock QPrintDialog so no real
printer is touched. The goal is to verify menu wiring and dispatch logic.
"""

import pytest
from PyQt6.QtWidgets import QDialog, QWidget
from qterminator.plugins.print_terminal import PrintPlugin


class FakeInnerTerm(QWidget):
    def __init__(self):
        super().__init__()
        self.resize(120, 80)

    def setSelectionStart(self, x, y):
        pass

    def setSelectionEnd(self, x, y):
        pass

    def selectedText(self):
        return "some buffer text"

    def clearSelection(self):
        pass

    def historyLinesCount(self):
        return 0


class FakeTerminal(QWidget):
    def __init__(self):
        super().__init__()
        self.resize(200, 120)
        self._term = FakeInnerTerm()
        self._term.setParent(self)

    def title(self):
        return "term"

    def selected_text(self):
        return ""


@pytest.fixture
def plugin():
    return PrintPlugin()


@pytest.fixture
def fake_terminal(qtbot):
    t = FakeTerminal()
    qtbot.addWidget(t)
    return t


def test_plugin_metadata(plugin):
    assert plugin.name == "print_terminal"
    assert plugin.version == "1.0"
    assert "menu_provider" in plugin.capabilities


def test_menu_items(plugin, fake_terminal):
    items = plugin.get_menu_items(fake_terminal)
    labels = [label for label, _ in items]
    assert "Print Terminal..." in labels
    assert "Print Buffer..." in labels
    assert len(items) == 2
    for _, cb in items:
        assert callable(cb)


def test_print_visible_cancel(plugin, fake_terminal, monkeypatch):
    """If the user rejects the print dialog, nothing further happens."""

    class FakeDialog:
        def __init__(self, *a, **kw):
            pass

        def exec(self):
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(
        "qterminator.plugins.print_terminal.QPrintDialog", FakeDialog,
    )
    # Must not raise.
    plugin._make_print_visible(fake_terminal)()


def test_print_buffer_cancel(plugin, fake_terminal, monkeypatch):
    class FakeDialog:
        def __init__(self, *a, **kw):
            pass

        def exec(self):
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(
        "qterminator.plugins.print_terminal.QPrintDialog", FakeDialog,
    )
    plugin._make_print_buffer(fake_terminal)()


def test_print_visible_accept_paints_to_pdf_printer(
    plugin, fake_terminal, tmp_path, monkeypatch,
):
    """Accept the dialog and redirect the QPrinter to PDF output, so we can
    verify the paint pipeline runs end-to-end without a real printer."""
    from PyQt6.QtPrintSupport import QPrinter

    out = tmp_path / "visible.pdf"

    class FakeDialog:
        def __init__(self, printer, parent=None):
            # Redirect to PDF so we get a file instead of hitting a printer.
            printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
            printer.setOutputFileName(str(out))

        def exec(self):
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(
        "qterminator.plugins.print_terminal.QPrintDialog", FakeDialog,
    )
    plugin._make_print_visible(fake_terminal)()
    assert out.exists()
    assert out.stat().st_size > 100


def test_print_buffer_accept_paints_to_pdf_printer(
    plugin, fake_terminal, tmp_path, monkeypatch,
):
    from PyQt6.QtPrintSupport import QPrinter

    out = tmp_path / "buffer.pdf"

    class FakeDialog:
        def __init__(self, printer, parent=None):
            printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
            printer.setOutputFileName(str(out))

        def exec(self):
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(
        "qterminator.plugins.print_terminal.QPrintDialog", FakeDialog,
    )
    plugin._make_print_buffer(fake_terminal)()
    assert out.exists()
    assert out.stat().st_size > 100


def test_print_buffer_empty_short_circuits(
    plugin, fake_terminal, monkeypatch,
):
    """If buffer text is empty, QPrintDialog should not be shown."""
    dialog_called = []

    class FakeDialog:
        def __init__(self, *a, **kw):
            dialog_called.append(True)

        def exec(self):
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(
        "qterminator.plugins.print_terminal.QPrintDialog", FakeDialog,
    )
    # Short-circuit _buffer_text to return empty.
    monkeypatch.setattr(
        "qterminator.plugins.print_terminal._buffer_text", lambda t: "",
    )
    monkeypatch.setattr(
        "qterminator.plugins.print_terminal.QMessageBox.information",
        lambda *a, **kw: None,
    )
    plugin._make_print_buffer(fake_terminal)()
    assert dialog_called == []
