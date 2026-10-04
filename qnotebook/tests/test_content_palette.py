"""Document code/link colors follow presentation without the font opt-in."""

from __future__ import annotations

from dataclasses import replace

import pytest
from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtGui import (
    QPalette,
    QTextBlockFormat,
    QTextCharFormat,
    QTextDocument,
    QTextFormat,
)
from qdistro_presentation.model import contrast_ratio, example_snapshot, with_generation
from qdistro_presentation.paths import ENV_OVERRIDE
from qdistro_presentation.publish import write_snapshot
from qnotebook.content_style import (
    ContentPresentationHighlighter,
    ContentStyle,
    desktop_content_style,
    document_palette,
    document_palette_colors,
    legacy_content_style,
    resolve_content_style,
    search_highlight_format,
)
from qnotebook.editor import MarkdownEditor, native_body_font, reset_pinned_body_font_for_tests
from qnotebook.export import export_page_html
from qnotebook.md_to_qdoc import (
    BLOCK_KIND,
    BLOCK_TOC_MARKER,
    BLOCK_TRANSCLUDED_CHILD,
    BLOCK_TRANSCLUSION,
    CHAR_CODE,
    CHAR_IMAGE_LINK,
    CHAR_TAG,
    CHAR_WIKILINK,
    markdown_to_qdoc,
)
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
    return [fmt for fmt, _bfmt in _record_presented(doc, style)]


def _record_presented(
    doc: QTextDocument, style: ContentStyle
) -> list[tuple[QTextCharFormat, QTextBlockFormat]]:
    recorded: list[tuple[QTextCharFormat, QTextBlockFormat]] = []

    class _Recorder(ContentPresentationHighlighter):
        def setFormat(self, start, count, fmt):  # noqa: N802
            recorded.append(
                (QTextCharFormat(fmt), QTextBlockFormat(self.currentBlock().blockFormat()))
            )
            super().setFormat(start, count, fmt)

    hi = _Recorder(doc)
    hi.set_style(style)
    return recorded


def test_legacy_export_style_has_no_live_palette(qapp):
    style = legacy_content_style()
    assert style.code_background is None
    assert style.link_color is None
    assert style.wiki_link_color is None
    assert style.tag_color is None
    assert style.dim_color is None
    assert style.image_link_color is None
    assert style.highlight_background is None
    assert style.highlight_foreground is None
    assert style.equation_background is None
    assert style.equation_foreground is None
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
    assert style.wiki_link_color == snap.colors.mSecondary
    assert style.tag_color == snap.colors.mTertiary
    assert style.dim_color == snap.colors.mOnSurfaceVariant
    assert style.image_link_color == snap.colors.mTertiary
    assert style.highlight_background == snap.colors.mPrimary
    assert style.highlight_foreground == snap.colors.mOnPrimary
    assert style.equation_background == snap.colors.mSecondary
    assert style.equation_foreground == snap.colors.mOnSecondary
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


def _alt_snapshot():
    return with_generation(
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


def _code_extra_colors(editor):
    colors = []
    for sel in editor.extraSelections():
        if sel.format.property(int(QTextFormat.Property.FullWidthSelection)):
            colors.append(
                (
                    sel.cursor.block().position(),
                    sel.cursor.block().text(),
                    sel.format.background().color().name(),
                )
            )
    return colors


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


def test_fenced_blank_line_gets_full_width_selection(
    qapp, tmp_path, qtbot, monkeypatch,
):
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    attach_presentation(qapp, _adapter("system"))
    ed = MarkdownEditor()
    qtbot.addWidget(ed)
    ed.load_markdown("```\nhello\n\nworld\n```\n")
    expected = example_snapshot().colors.mSurfaceVariant
    extras = _code_extra_colors(ed)
    texts = [text for _pos, text, color in extras if color == expected]
    assert "hello" in texts
    assert "world" in texts
    assert "" in texts
    code_blocks = 0
    empty_code = 0
    block = ed.document().firstBlock()
    while block.isValid():
        if str(block.blockFormat().property(BLOCK_KIND) or "") == "code":
            code_blocks += 1
            if block.text() == "":
                empty_code += 1
        block = block.next()
    assert empty_code >= 1
    assert len(extras) == code_blocks
    ed.setExtraSelections([])
    extras_after = _code_extra_colors(ed)
    assert len(extras_after) == code_blocks
    assert all(color == expected for _pos, _text, color in extras_after)


def test_live_fenced_blank_line_restyle_updates_block_area(
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
    md = "```\nhello\n\nworld\n```\n"
    ed.load_markdown(md, page_path=page)
    first = example_snapshot().colors.mSurfaceVariant
    extras = _code_extra_colors(ed)
    assert any(text == "" and color == first for _pos, text, color in extras)
    ed.insert_text_at_cursor("Z")
    stack_before = ed.document().availableUndoSteps()
    md_before = ed.markdown()
    write_snapshot(str(tmp_path), _alt_snapshot(), require_unwritable_dirs=False)
    current_controller()._reload()
    win.apply_presentation_update()
    assert ed.markdown() == md_before
    assert ed.is_dirty()
    assert ed.document().availableUndoSteps() == stack_before
    extras = _code_extra_colors(ed)
    assert any(text == "" and color == "#222244" for _pos, text, color in extras)
    assert all(color == "#222244" for _pos, _text, color in extras)
    assert first not in {color for _pos, _text, color in extras}
    win.close()


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

    write_snapshot(str(tmp_path), _alt_snapshot(), require_unwritable_dirs=False)
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
    assert "#1c71d8" in html
    win.close()


def _role_style(**colors) -> ContentStyle:
    return ContentStyle(
        body_family="DejaVu Sans",
        body_point_size=11,
        code_family="monospace",
        code_point_size=11,
        inherit_desktop=False,
        **colors,
    )


def _stored_named(doc: QTextDocument, predicate) -> list[str]:
    names = []
    block = doc.firstBlock()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            fmt = frag.charFormat()
            if predicate(fmt, block):
                names.append(fmt.foreground().color().name())
            it += 1
        block = block.next()
    return names


def test_highlighter_paints_wiki_and_tag_from_style(qapp):
    doc = QTextDocument()
    markdown_to_qdoc(
        "see [[Home]] and #todo\n",
        doc,
        content_style=legacy_content_style(),
    )
    wiki_stored = _stored_named(
        doc, lambda fmt, _b: bool(fmt.property(CHAR_WIKILINK))
    )
    tag_stored = _stored_named(doc, lambda fmt, _b: bool(fmt.property(CHAR_TAG)))
    assert wiki_stored
    assert tag_stored
    assert all(color == "#1a5fb4" for color in wiki_stored)
    assert all(color == "#1c71d8" for color in tag_stored)

    style = _role_style(wiki_link_color="#a9aefe", tag_color="#9bfece")
    recorded = _record_formats(doc, style)
    wiki_fg = [
        fmt.foreground().color().name()
        for fmt in recorded
        if bool(fmt.property(CHAR_WIKILINK))
    ]
    tag_fg = [
        fmt.foreground().color().name()
        for fmt in recorded
        if bool(fmt.property(CHAR_TAG))
    ]
    assert wiki_fg
    assert tag_fg
    assert all(color == "#a9aefe" for color in wiki_fg)
    assert all(color == "#9bfece" for color in tag_fg)
    assert all(color == "#1a5fb4" for color in wiki_stored)
    assert all(color == "#1c71d8" for color in tag_stored)


def test_highlighter_without_wiki_role_keeps_parse_color(qapp):
    doc = QTextDocument()
    markdown_to_qdoc("see [[Home]]\n", doc, content_style=legacy_content_style())
    recorded = _record_formats(doc, _role_style())
    wiki_fg = [
        fmt.foreground().color().name()
        for fmt in recorded
        if bool(fmt.property(CHAR_WIKILINK))
    ]
    assert wiki_fg
    assert all(color == "#1a5fb4" for color in wiki_fg)


def test_highlighter_paints_toc_and_transclusion_from_style(qapp):
    toc = QTextDocument()
    markdown_to_qdoc("# Hello\n\n[[!TOC]]\n", toc, content_style=legacy_content_style())
    toc_stored = _stored_named(
        toc, lambda _fmt, block: bool(block.blockFormat().property(BLOCK_TOC_MARKER))
    )
    assert toc_stored
    assert all(color == "#1a5fb4" for color in toc_stored)
    toc_recorded = _record_presented(toc, _role_style(wiki_link_color="#a9aefe"))
    marker_fg = [
        fmt.foreground().color().name()
        for fmt, bfmt in toc_recorded
        if bool(bfmt.property(BLOCK_TOC_MARKER)) and not bool(fmt.property(CHAR_WIKILINK))
    ]
    heading_fg = [
        fmt.foreground().color().name()
        for fmt, _bfmt in toc_recorded
        if bool(fmt.property(CHAR_WIKILINK))
        or (fmt.isAnchor() and (fmt.anchorHref() or "").startswith("qnotebook:#"))
    ]
    assert marker_fg
    assert heading_fg
    assert all(color == "#a9aefe" for color in marker_fg)
    assert all(color == "#a9aefe" for color in heading_fg)

    trans = QTextDocument()
    markdown_to_qdoc(
        "{{Foo}}\n",
        trans,
        transclusion_resolver=lambda _t: "included body",
        content_style=legacy_content_style(),
    )
    marker_stored = _stored_named(
        trans, lambda _fmt, block: bool(block.blockFormat().property(BLOCK_TRANSCLUSION))
    )
    child_stored = _stored_named(
        trans,
        lambda _fmt, block: bool(block.blockFormat().property(BLOCK_TRANSCLUDED_CHILD)),
    )
    assert marker_stored
    assert child_stored
    assert all(color == "#7f7f7f" for color in marker_stored)
    assert all(color == "#4a4a4a" for color in child_stored)
    trans_recorded = _record_presented(trans, _role_style(dim_color="#7c80b4"))
    placeholder_fg = [
        fmt.foreground().color().name()
        for fmt, bfmt in trans_recorded
        if bool(bfmt.property(BLOCK_TRANSCLUSION))
    ]
    child_fg = [
        fmt.foreground().color().name()
        for fmt, bfmt in trans_recorded
        if bool(bfmt.property(BLOCK_TRANSCLUDED_CHILD))
    ]
    assert placeholder_fg
    assert child_fg
    assert all(color == "#7c80b4" for color in placeholder_fg)
    assert all(color == "#7c80b4" for color in child_fg)
    assert all(color == "#7f7f7f" for color in marker_stored)
    assert all(color == "#4a4a4a" for color in child_stored)


def test_highlighter_paints_equation_pair_from_style(qapp):
    doc = QTextDocument()
    markdown_to_qdoc("Inline $E=mc^2$ here.\n", doc, content_style=legacy_content_style())
    from qnotebook.equations import EQ_LATEX

    eq_bgs = []
    block = doc.firstBlock()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            fmt = it.fragment().charFormat()
            if fmt.property(EQ_LATEX):
                eq_bgs.append(fmt.background().color().name())
            it += 1
        block = block.next()
    assert eq_bgs
    assert all(color == "#fff8dc" for color in eq_bgs)

    style = _role_style(
        code_background="#11112d",
        equation_background="#a9aefe",
        equation_foreground="#0e0e43",
    )
    recorded = _record_formats(doc, style)
    eq_recorded = [fmt for fmt in recorded if fmt.property(EQ_LATEX)]
    assert eq_recorded
    assert all(fmt.background().color().name() == "#a9aefe" for fmt in eq_recorded)
    assert all(fmt.foreground().color().name() == "#0e0e43" for fmt in eq_recorded)
    assert all(fmt.background().color().name() != "#11112d" for fmt in eq_recorded)


def test_highlighter_paints_image_markup_from_style(qapp, qtbot):
    ed = MarkdownEditor()
    qtbot.addWidget(ed)
    ed.load_markdown("")
    ed.set_live_reparse_enabled(True)
    cur = ed.textCursor()
    cur.insertText("![alt](_resources/x.png)")
    ed.live_reparse_now()
    stored = None
    block = ed.document().firstBlock()
    it = block.begin()
    while not it.atEnd():
        fmt = it.fragment().charFormat()
        if fmt.property(CHAR_IMAGE_LINK):
            stored = fmt.foreground().color().name()
        it += 1
    assert stored == "#7c3aed"
    recorded = _record_formats(ed.document(), _role_style(image_link_color="#9bfece"))
    image_fg = [
        fmt.foreground().color().name()
        for fmt in recorded
        if fmt.property(CHAR_IMAGE_LINK)
    ]
    assert image_fg
    assert all(color == "#9bfece" for color in image_fg)
    assert stored == "#7c3aed"
    ed.deleteLater()


def test_search_highlight_uses_paired_foreground(qapp, tmp_path, tmp_notebook, qtbot, monkeypatch):
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    attach_presentation(qapp, _adapter("system"))
    win = MainWindow()
    win.open_notebook(str(tmp_notebook))
    qtbot.addWidget(win)
    win.load_page("Home")
    win._highlight_all_occurrences("Welcome")
    snap = example_snapshot()
    sels = win.editor.extraSelections()
    assert sels
    bgs = {sel.format.background().color().name() for sel in sels}
    fgs = {sel.format.foreground().color().name() for sel in sels}
    assert snap.colors.mPrimary in bgs
    assert snap.colors.mOnPrimary in fgs
    fmt = search_highlight_format(resolve_content_style())
    assert fmt.background().color().name() == snap.colors.mPrimary
    assert fmt.foreground().color().name() == snap.colors.mOnPrimary
    win.close()


def test_search_highlight_without_palette_keeps_legacy_yellow(qapp):
    reset_controller_for_tests()
    fmt = search_highlight_format(legacy_content_style())
    assert fmt.background().color().name() == "#fff48a"
    assert fmt.foreground().style() == Qt.BrushStyle.NoBrush


def test_snapshot_document_roles_meet_contrast(qapp, tmp_path, monkeypatch):
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    attach_presentation(qapp, _adapter("system"))
    pal = document_palette()
    snap = example_snapshot()
    surface = snap.colors.mSurface
    assert pal.wiki_link_color == snap.colors.mSecondary
    assert pal.tag_color == snap.colors.mTertiary
    assert pal.dim_color == snap.colors.mOnSurfaceVariant
    assert contrast_ratio(pal.wiki_link_color, surface) >= 3.0
    assert contrast_ratio(pal.tag_color, surface) >= 3.0
    assert contrast_ratio(pal.dim_color, surface) >= 3.0
    assert contrast_ratio(pal.image_link_color, surface) >= 3.0
    assert contrast_ratio(pal.link_color, surface) >= 3.0
    assert contrast_ratio(pal.highlight_foreground, pal.highlight_background) >= 4.5
    assert contrast_ratio(pal.equation_foreground, pal.equation_background) >= 4.5


def test_live_restyle_updates_wiki_not_markdown(
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
    ed.load_markdown("see [[Home]] and #todo\n", page_path=page)
    first_wiki = example_snapshot().colors.mSecondary
    recorded: list[QTextCharFormat] = []

    class _Recorder(ContentPresentationHighlighter):
        def setFormat(self, start, count, fmt):  # noqa: N802
            recorded.append(QTextCharFormat(fmt))
            super().setFormat(start, count, fmt)

    ed._content_highlighter.setDocument(None)
    ed._content_highlighter = _Recorder(ed.document())
    ed.apply_content_presentation()
    wiki_fg = [
        fmt.foreground().color().name()
        for fmt in recorded
        if bool(fmt.property(CHAR_WIKILINK))
    ]
    assert first_wiki in wiki_fg
    ed.insert_text_at_cursor("Z")
    stack_before = ed.document().availableUndoSteps()
    md_before = ed.markdown()
    write_snapshot(
        str(tmp_path),
        with_generation(
            replace(
                example_snapshot(),
                colors=replace(
                    example_snapshot().colors,
                    mSecondary="#c5cae9",
                    mTertiary="#80cbc4",
                ),
            )
        ),
        require_unwritable_dirs=False,
    )
    current_controller()._reload()
    recorded.clear()
    win.apply_presentation_update()
    assert ed.markdown() == md_before
    assert ed.is_dirty()
    assert ed.document().availableUndoSteps() == stack_before
    wiki_fg = [
        fmt.foreground().color().name()
        for fmt in recorded
        if bool(fmt.property(CHAR_WIKILINK))
    ]
    tag_fg = [
        fmt.foreground().color().name()
        for fmt in recorded
        if bool(fmt.property(CHAR_TAG))
    ]
    assert "#c5cae9" in wiki_fg
    assert first_wiki not in wiki_fg
    assert "#80cbc4" in tag_fg
    win.close()
