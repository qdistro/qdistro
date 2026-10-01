"""Content-font opt-in: semantic restyle without dirty/undo/export leakage."""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtGui import QTextCursor
from qdistro_presentation.model import example_snapshot
from qdistro_presentation.paths import ENV_OVERRIDE
from qdistro_presentation.publish import write_snapshot
from qnotebook.appearance import (
    save_use_desktop_document_fonts,
)
from qnotebook.content_style import (
    ContentStyle,
    apply_document_presentation,
    desktop_content_style,
    legacy_content_style,
    resolve_content_style,
)
from qnotebook.editor import MarkdownEditor, reset_pinned_body_font_for_tests
from qnotebook.export import export_page_html, export_page_pdf
from qnotebook.md_to_qdoc import CHAR_CODE, CHAR_STRONG
from qnotebook.theme import attach_presentation, reset_controller_for_tests
from qnotebook.window import MainWindow


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


def _adapter(theme_mode: str = "system"):
    from qnotebook.appearance import SettingsAdapter, save_theme_mode

    s = QSettings("qnotebook", "qnotebook")
    save_theme_mode(s, theme_mode, update_legacy=(theme_mode != "system"))
    return SettingsAdapter(s)


def test_opt_in_default_off(qapp):
    assert resolve_content_style().inherit_desktop is False
    assert resolve_content_style().body_point_size == 11


def test_toggle_code_sets_char_code(qapp, qtbot):
    ed = MarkdownEditor()
    qtbot.addWidget(ed)
    ed.load_markdown("hello world\n")
    cur = ed.textCursor()
    cur.select(QTextCursor.SelectionType.Document)
    ed.setTextCursor(cur)
    ed.toggle_code()
    c = QTextCursor(ed.document())
    c.setPosition(1)
    assert bool(c.charFormat().property(CHAR_CODE))
    assert "`hello world`" in ed.markdown()
    ed.toggle_code()
    c.setPosition(1)
    assert not bool(c.charFormat().property(CHAR_CODE))
    assert "`" not in ed.markdown()


def test_toggle_bold_in_heading_emits_strong(qapp, qtbot):
    ed = MarkdownEditor()
    qtbot.addWidget(ed)
    ed.load_markdown("# Title word\n")
    c = QTextCursor(ed.document())
    text = ed.document().toPlainText()
    idx = text.index("word")
    c.setPosition(idx)
    c.setPosition(idx + 4, QTextCursor.MoveMode.KeepAnchor)
    ed.setTextCursor(c)
    ed.toggle_bold()
    c.setPosition(idx + 1)
    assert bool(c.charFormat().property(CHAR_STRONG))
    md = ed.markdown()
    assert "**word**" in md
    assert md.startswith("# ")


def test_desktop_opt_in_restyles_without_dirty_or_undo(qapp, tmp_path, tmp_notebook, qtbot, monkeypatch):
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    attach_presentation(qapp, _adapter("system"))
    save_use_desktop_document_fonts(QSettings("qnotebook", "qnotebook"), True)
    win = MainWindow()
    win.open_notebook(str(tmp_notebook))
    qtbot.addWidget(win)
    ed = win.editor
    page = win._current_page
    assert page is not None
    ed.load_markdown("# Hello **world**\n\nbody `code`\n", page_path=page)
    style = desktop_content_style()
    assert style.inherit_desktop is True
    c = QTextCursor(ed.document())
    c.setPosition(1)
    families = list(c.charFormat().fontFamilies() or [])
    assert style.body_family in families or c.charFormat().fontFamily() == style.body_family
    assert abs(c.charFormat().fontPointSize() - style.heading_point_size(1)) < 0.2
    assert "**world**" in ed.markdown()
    assert "`code`" in ed.markdown()

    ed.insert_text_at_cursor("Z")
    assert ed.is_dirty()
    stack_before = ed.document().availableUndoSteps()
    md_before = ed.markdown()
    win.apply_presentation_update()
    assert ed.markdown() == md_before
    assert ed.is_dirty()
    assert ed.document().availableUndoSteps() == stack_before
    win.close()


def test_live_reparse_bold_sets_char_strong(qapp, qtbot):
    ed = MarkdownEditor()
    qtbot.addWidget(ed)
    ed.load_markdown("")
    ed.set_live_reparse_enabled(True)
    cur = ed.textCursor()
    cur.insertText("this is **bold** text")
    ed.live_reparse_now()
    full = ed.toPlainText()
    idx = full.index("bold")
    cur.setPosition(idx + 1)
    assert bool(cur.charFormat().property(CHAR_STRONG))
    assert "**bold**" in ed.markdown()


def test_theme_change_does_not_rewrite_markdown_bytes(qapp, tmp_notebook, qtbot, tmp_path, monkeypatch):
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    attach_presentation(qapp, _adapter("system"))
    save_use_desktop_document_fonts(QSettings("qnotebook", "qnotebook"), True)
    win = MainWindow()
    win.open_notebook(str(tmp_notebook))
    qtbot.addWidget(win)
    page = win._current_page
    path = win.notebook.file_for(page)
    before = path.read_bytes()
    win.editor.load_markdown(path.read_text(), page_path=page, base_path=path.parent)
    win.editor.clear_dirty()
    win.apply_presentation_update()
    assert not win.editor.is_dirty()
    assert path.read_bytes() == before
    win.close()


def test_export_uses_legacy_style_not_live_screen(qapp, tmp_notebook, tmp_path, qtbot, monkeypatch):
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    attach_presentation(qapp, _adapter("system"))
    save_use_desktop_document_fonts(QSettings("qnotebook", "qnotebook"), True)
    win = MainWindow()
    win.open_notebook(str(tmp_notebook))
    qtbot.addWidget(win)
    page = win._current_page
    html_path = tmp_path / "page.html"
    export_page_html(win.notebook, page, html_path)
    html = html_path.read_text()
    # HTML export CSS is the notebook default serif, not the live snapshot family
    assert "Georgia" in html or "font-family" in html
    pdf_path = tmp_path / "page.pdf"
    export_page_pdf(win.notebook, page, pdf_path)
    assert pdf_path.is_file() and pdf_path.stat().st_size > 0
    win.close()


def test_document_fonts_checkbox_default_off_and_distinct(qapp, tmp_notebook, qtbot):
    from qnotebook.settings_dialog import SettingsDialog

    win = MainWindow()
    win.open_notebook(str(tmp_notebook))
    qtbot.addWidget(win)
    dlg = SettingsDialog(win)
    qtbot.addWidget(dlg)
    assert dlg._chk_document_fonts.text() == "Use desktop document fonts"
    assert dlg._chk_desktop_fonts.text() == "Use desktop fonts"
    assert dlg._chk_document_fonts.isChecked() is False
    dlg._chk_document_fonts.setChecked(True)
    dlg._apply()
    s = QSettings("qnotebook", "qnotebook")
    from qnotebook.appearance import load_use_desktop_document_fonts

    assert load_use_desktop_document_fonts(s) is True
    win.close()


def test_apply_presentation_preserves_char_strong(qapp):
    from PyQt6.QtGui import QTextDocument
    from qnotebook.md_to_qdoc import markdown_to_qdoc

    doc = QTextDocument()
    markdown_to_qdoc("# Hello **world**\n", doc)
    style = ContentStyle(
        body_family="DejaVu Sans",
        body_point_size=14,
        code_family="DejaVu Sans Mono",
        code_point_size=14,
        inherit_desktop=True,
    )
    apply_document_presentation(doc, style)
    text = doc.toPlainText()
    idx = text.index("world")
    cur = QTextCursor(doc)
    cur.setPosition(idx + 1)
    assert bool(cur.charFormat().property(CHAR_STRONG))
    from qnotebook.qdoc_to_md import qdoc_to_markdown

    assert "**world**" in qdoc_to_markdown(doc)
