"""Document code/link colors follow presentation without the font opt-in."""

from __future__ import annotations

from dataclasses import replace

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtGui import QPalette, QTextCharFormat, QTextDocument
from qdistro_presentation.model import example_snapshot, with_generation
from qdistro_presentation.paths import ENV_OVERRIDE
from qdistro_presentation.publish import write_snapshot
from qnotebook.content_style import (
    ContentPresentationHighlighter,
    ContentStyle,
    desktop_content_style,
    document_palette_colors,
    legacy_content_style,
    resolve_content_style,
)
from qnotebook.editor import MarkdownEditor, native_body_font, reset_pinned_body_font_for_tests
from qnotebook.export import export_page_html
from qnotebook.md_to_qdoc import CHAR_CODE, markdown_to_qdoc
from qnotebook.theme import attach_presentation, current_controller, reset_controller_for_tests
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


def _record_formats(doc: QTextDocument, style: ContentStyle) -> list[QTextCharFormat]:
    recorded: list[QTextCharFormat] = []

    class _Recorder(ContentPresentationHighlighter):
        def setFormat(self, start, count, fmt):  # noqa: N802
            recorded.append(QTextCharFormat(fmt))
            super().setFormat(start, count, fmt)

    hi = _Recorder(doc)
    hi.set_style(style)
    return recorded


def test_legacy_export_style_has_no_live_palette(qapp):
    style = legacy_content_style()
    assert style.code_background is None
    assert style.link_color is None
    assert style.inherit_desktop is False


def test_palette_without_controller_leaves_hardcoded_colors(qapp):
    reset_controller_for_tests()
    reset_pinned_body_font_for_tests()
    assert document_palette_colors() == (None, None)
    style = resolve_content_style()
    assert style.inherit_desktop is False
    assert style.code_background is None
    assert style.link_color is None


def test_follow_desktop_palette_without_font_opt_in(qapp, tmp_path, monkeypatch):
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    attach_presentation(qapp, _adapter("system"))
    snap = example_snapshot()
    style = resolve_content_style()
    assert style.inherit_desktop is False
    assert style.body_point_size == 11
    assert style.body_family == native_body_font().family()
    assert style.code_background == snap.colors.mSurfaceVariant
    assert style.link_color == snap.colors.mPrimary
    desktop = desktop_content_style()
    assert desktop.inherit_desktop is True
    assert desktop.code_background == snap.colors.mSurfaceVariant


def test_explicit_dark_uses_application_palette_not_snapshot(qapp, tmp_path, monkeypatch):
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    attach_presentation(qapp, _adapter("dark"))
    snap = example_snapshot()
    style = resolve_content_style()
    assert current_controller().theme_mode == "dark"
    assert current_controller().state.colors is None
    assert style.inherit_desktop is False
    alt = qapp.palette().color(QPalette.ColorRole.AlternateBase).name()
    link = qapp.palette().color(QPalette.ColorRole.Link).name()
    assert style.code_background == alt
    assert style.link_color == link
    assert style.code_background != snap.colors.mSurfaceVariant


def test_highlighter_paints_code_and_link_from_style(qapp):
    doc = QTextDocument()
    markdown_to_qdoc(
        "see `code` and [site](https://example.com)\n",
        doc,
        content_style=legacy_content_style(),
    )
    stored_code = None
    stored_link = None
    block = doc.firstBlock()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            fmt = frag.charFormat()
            if bool(fmt.property(CHAR_CODE)):
                stored_code = fmt.background().color().name()
            if fmt.isAnchor() and (fmt.anchorHref() or "").startswith("https:"):
                stored_link = fmt.foreground().color().name()
            it += 1
        block = block.next()
    assert stored_code == "#f4f4f4"
    assert stored_link == "#1a5fb4"

    style = ContentStyle(
        body_family="DejaVu Sans",
        body_point_size=11,
        code_family="monospace",
        code_point_size=11,
        inherit_desktop=False,
        code_background="#11112d",
        link_color="#fff59b",
    )
    recorded = _record_formats(doc, style)
    bgs = {fmt.background().color().name() for fmt in recorded}
    fgs = {fmt.foreground().color().name() for fmt in recorded}
    assert "#11112d" in bgs
    assert "#fff59b" in fgs
    assert stored_code == "#f4f4f4"
    assert stored_link == "#1a5fb4"


def test_highlighter_without_palette_keeps_parse_colors(qapp):
    doc = QTextDocument()
    markdown_to_qdoc("see `code`\n", doc, content_style=legacy_content_style())
    style = ContentStyle(
        body_family="DejaVu Sans",
        body_point_size=11,
        code_family="monospace",
        code_point_size=11,
        inherit_desktop=False,
    )
    recorded = _record_formats(doc, style)
    code_bgs = [
        fmt.background().color().name()
        for fmt in recorded
        if bool(fmt.property(CHAR_CODE))
    ]
    assert code_bgs
    assert all(color == "#f4f4f4" for color in code_bgs)


def test_highlighter_paints_fenced_code_block(qapp):
    doc = QTextDocument()
    markdown_to_qdoc("```\nhello\n```\n", doc, content_style=legacy_content_style())
    style = ContentStyle(
        body_family="DejaVu Sans",
        body_point_size=11,
        code_family="monospace",
        code_point_size=11,
        inherit_desktop=False,
        code_background="#11112d",
    )
    recorded = _record_formats(doc, style)
    bgs = {fmt.background().color().name() for fmt in recorded}
    assert "#11112d" in bgs


def test_highlighter_leaves_equation_gold(qapp):
    doc = QTextDocument()
    markdown_to_qdoc("Inline $E=mc^2$ here.\n", doc, content_style=legacy_content_style())
    style = ContentStyle(
        body_family="DejaVu Sans",
        body_point_size=11,
        code_family="monospace",
        code_point_size=11,
        inherit_desktop=False,
        code_background="#11112d",
    )
    recorded = _record_formats(doc, style)
    from qnotebook.equations import EQ_LATEX

    eq_bgs = [
        fmt.background().color().name()
        for fmt in recorded
        if fmt.property(EQ_LATEX)
    ]
    assert eq_bgs
    assert all(color == "#fff8dc" for color in eq_bgs)
    assert "#11112d" not in eq_bgs


def test_live_restyle_changes_highlighter_not_markdown(
    qapp, tmp_path, tmp_notebook, qtbot, monkeypatch,
):
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    attach_presentation(qapp, _adapter("system"))
    win = MainWindow()
    win.open_notebook(str(tmp_notebook))
    qtbot.addWidget(win)
    ed = win.editor
    page = win._current_page
    ed.load_markdown("see `code` and [site](https://example.com)\n", page_path=page)
    first = example_snapshot().colors.mSurfaceVariant
    recorded: list[QTextCharFormat] = []

    class _Recorder(ContentPresentationHighlighter):
        def setFormat(self, start, count, fmt):  # noqa: N802
            recorded.append(QTextCharFormat(fmt))
            super().setFormat(start, count, fmt)

    ed._content_highlighter.setDocument(None)
    ed._content_highlighter = _Recorder(ed.document())
    ed.apply_content_presentation()
    assert first in {fmt.background().color().name() for fmt in recorded}
    assert ed.font().family() == native_body_font().family()
    assert ed.font().pointSize() == 11

    ed.insert_text_at_cursor("Z")
    assert ed.is_dirty()
    stack_before = ed.document().availableUndoSteps()
    md_before = ed.markdown()

    other = with_generation(
        replace(
            example_snapshot(),
            colors=replace(
                example_snapshot().colors,
                mSurfaceVariant="#222244",
                mOnSurfaceVariant="#c5cae9",
                mPrimary="#c5cae9",
                mOnPrimary="#0e0e43",
            ),
        )
    )
    write_snapshot(str(tmp_path), other, require_unwritable_dirs=False)
    current_controller()._reload()
    recorded.clear()
    win.apply_presentation_update()
    assert ed.markdown() == md_before
    assert ed.is_dirty()
    assert ed.document().availableUndoSteps() == stack_before
    bgs = {fmt.background().color().name() for fmt in recorded}
    assert "#222244" in bgs
    assert first not in bgs
    win.close()


def test_export_html_keeps_baseline_not_snapshot_surface(
    qapp, tmp_notebook, tmp_path, qtbot, monkeypatch,
):
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    attach_presentation(qapp, _adapter("system"))
    win = MainWindow()
    win.open_notebook(str(tmp_notebook))
    qtbot.addWidget(win)
    html_path = tmp_path / "page.html"
    export_page_html(win.notebook, win._current_page, html_path)
    html = html_path.read_text()
    assert example_snapshot().colors.mSurfaceVariant not in html
    assert "#f0efe9" in html
    assert "#1a5fb4" in html
    win.close()
