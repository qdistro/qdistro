"""Editor zoom: a view-only document font scale (plan 06 item 3).

Zoom multiplies the resolved document fonts (native or shared) on screen
only. It must not reach Markdown, char formats, the dirty flag, undo, or
PDF export, and it applies to every editor pane.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QPoint, QPointF, QSettings, Qt
from PyQt6.QtGui import QTextCharFormat, QTextCursor, QWheelEvent
from PyQt6.QtWidgets import QApplication
from qnotebook.appearance import (
    ZOOM_MAX_PERCENT,
    ZOOM_MIN_PERCENT,
    load_editor_zoom,
    save_editor_zoom,
    save_use_desktop_document_fonts,
)
from qnotebook.content_style import legacy_content_style, resolve_content_style
from qnotebook.editor import reset_pinned_body_font_for_tests
from qnotebook.export import export_page_pdf
from qnotebook.theme import reset_controller_for_tests
from qnotebook.window import MainWindow

from .test_presentation_invariants import SAMPLE, _attach, _pdf_normalized, _snaps


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path_factory):
    d = tmp_path_factory.mktemp("qsettings")
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(d))
    s = QSettings("qnotebook", "qnotebook")
    s.clear()
    s.sync()
    yield
    reset_controller_for_tests()
    reset_pinned_body_font_for_tests()


def _rendered(ed, needle: str) -> QTextCharFormat:
    """Format painted at ``needle``: stored char format + highlighter overlay."""
    pos = ed.document().toPlainText().index(needle) + 1
    block = ed.document().findBlock(pos)
    rel = pos - block.position()
    c = QTextCursor(ed.document())
    c.setPosition(pos)
    fmt = QTextCharFormat(c.charFormat())
    for rng in block.layout().formats():
        if rng.start <= rel < rng.start + rng.length:
            fmt.merge(rng.format)
    return fmt


def _stored(ed, needle: str) -> float:
    pos = ed.document().toPlainText().index(needle) + 1
    c = QTextCursor(ed.document())
    c.setPosition(pos)
    return c.charFormat().fontPointSize()


def _window(tmp_notebook, qtbot):
    win = MainWindow()
    win.open_notebook(str(tmp_notebook))
    qtbot.addWidget(win)
    win.editor.load_markdown(SAMPLE, page_path=win._current_page)
    return win


NEEDLES = ("Body ", "Heading with", "inline code", "print(")


def _sizes(ed):
    return {n: _rendered(ed, n).fontPointSize() for n in NEEDLES}


@pytest.mark.parametrize("desktop", [False, True], ids=["native", "shared"])
def test_zoom_scales_rendered_fonts_only(qapp, tmp_path, tmp_notebook, qtbot, monkeypatch, desktop):
    if desktop:
        _attach(qapp, tmp_path, monkeypatch, _snaps()[1])
        save_use_desktop_document_fonts(QSettings("qnotebook", "qnotebook"), True)
    win = _window(tmp_notebook, qtbot)
    ed = win.editor
    assert resolve_content_style().inherit_desktop is desktop
    authored = ed.markdown()
    base = _sizes(ed)
    stored = {n: _stored(ed, n) for n in NEEDLES}
    undo_steps = ed.document().availableUndoSteps()
    assert not ed.is_dirty()

    assert win.set_editor_zoom(150) == 150
    zoomed = _sizes(ed)
    for n in NEEDLES:
        assert zoomed[n] == pytest.approx(base[n] * 1.5, abs=0.05), n
    # Heading still larger than body; code keeps its own base.
    assert zoomed["Heading with"] > zoomed["Body "]
    # View only: stored formats, Markdown, dirty flag and undo are untouched.
    assert {n: _stored(ed, n) for n in NEEDLES} == stored
    assert ed.markdown() == authored
    assert not ed.is_dirty()
    assert ed.document().availableUndoSteps() == undo_steps
    # The widget font (empty lines, caret) follows the zoom too.
    assert ed.font().pointSizeF() == pytest.approx(base["Body "] * 1.5, abs=0.05)

    win.set_editor_zoom(100)
    assert _sizes(ed) == pytest.approx(base)
    win.close()


def test_zoom_is_persisted_and_clamped(qapp):
    s = QSettings("qnotebook", "qnotebook")
    assert load_editor_zoom(s) == 100
    assert save_editor_zoom(s, 120) == 120
    assert load_editor_zoom(QSettings("qnotebook", "qnotebook")) == 120
    assert save_editor_zoom(s, 10) == ZOOM_MIN_PERCENT
    assert save_editor_zoom(s, 9999) == ZOOM_MAX_PERCENT
    s.setValue("editor/zoom_percent", "garbage")
    assert load_editor_zoom(s) == 100
    save_editor_zoom(s, 100)
    assert not s.contains("editor/zoom_percent")


def test_new_window_starts_at_saved_zoom(qapp, tmp_notebook, qtbot):
    save_editor_zoom(QSettings("qnotebook", "qnotebook"), 200)
    win = _window(tmp_notebook, qtbot)
    body = _rendered(win.editor, "Body ").fontPointSize()
    assert body == pytest.approx(legacy_content_style().body_point_size * 2, abs=0.05)
    win.close()


def test_shortcuts_reach_zoom_from_the_editor(qapp, tmp_notebook, qtbot):
    win = _window(tmp_notebook, qtbot)
    win.show()
    qtbot.waitExposed(win)
    win.editor.setFocus()
    qtbot.waitUntil(lambda: QApplication.focusWidget() is win.editor, timeout=2000)
    base = _rendered(win.editor, "Body ").fontPointSize()
    authored = win.editor.markdown()

    qtbot.keyClick(win.editor, Qt.Key.Key_Equal, Qt.KeyboardModifier.ControlModifier)
    assert load_editor_zoom() == 110
    qtbot.keyClick(win.editor, Qt.Key.Key_Minus, Qt.KeyboardModifier.ControlModifier)
    qtbot.keyClick(win.editor, Qt.Key.Key_Minus, Qt.KeyboardModifier.ControlModifier)
    assert load_editor_zoom() == 90
    assert _rendered(win.editor, "Body ").fontPointSize() == pytest.approx(base * 0.9, abs=0.05)
    qtbot.keyClick(win.editor, Qt.Key.Key_0, Qt.KeyboardModifier.ControlModifier)
    assert load_editor_zoom() == 100
    # The keys were shortcuts, not typed text.
    assert win.editor.markdown() == authored
    assert not win.editor.is_dirty()
    win.close()


def _wheel(widget, dy, modifiers):
    pos = QPointF(widget.viewport().rect().center())
    return QWheelEvent(
        pos, QPointF(widget.mapToGlobal(pos.toPoint())), QPoint(0, 0), QPoint(0, dy),
        Qt.MouseButton.NoButton, modifiers, Qt.ScrollPhase.NoScrollPhase, False,
    )


def test_ctrl_wheel_zooms_every_pane(qapp, tmp_notebook, qtbot):
    win = _window(tmp_notebook, qtbot)
    win.split_editor("horizontal")
    from qnotebook.editor import MarkdownEditor

    editors = win.findChildren(MarkdownEditor)
    assert len(editors) >= 2
    second = next(e for e in editors if e is not win.editor)
    second.load_markdown(SAMPLE)
    base = _rendered(second, "Body ").fontPointSize()
    widget_font = win.editor.font().pointSizeF()

    QApplication.sendEvent(win.editor.viewport(), _wheel(win.editor, 120, Qt.KeyboardModifier.ControlModifier))
    assert load_editor_zoom() == 110
    # Qt's own Ctrl+wheel zoom (widget font only) did not also run.
    assert win.editor.font().pointSizeF() == pytest.approx(widget_font * 1.1, abs=0.05)
    assert _rendered(second, "Body ").fontPointSize() == pytest.approx(base * 1.1, abs=0.05)

    # A plain wheel scrolls, it does not zoom.
    QApplication.sendEvent(win.editor.viewport(), _wheel(win.editor, -120, Qt.KeyboardModifier.NoModifier))
    assert load_editor_zoom() == 110
    win.close()


def test_pdf_export_ignores_zoom(qapp, tmp_path, tmp_notebook, qtbot):
    win = _window(tmp_notebook, qtbot)
    base_pdf = tmp_path / "base.pdf"
    export_page_pdf(win.notebook, win._current_page, base_pdf)
    win.set_editor_zoom(250)
    assert _rendered(win.editor, "Body ").fontPointSize() > 20
    zoomed_pdf = tmp_path / "zoomed.pdf"
    export_page_pdf(win.notebook, win._current_page, zoomed_pdf)
    win.close()
    assert _pdf_normalized(zoomed_pdf) == _pdf_normalized(base_pdf)
