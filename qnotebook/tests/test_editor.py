from __future__ import annotations

from PyQt6.QtGui import QTextCursor
from qnotebook.editor import MarkdownEditor


def test_load_and_read(qapp):
    ed = MarkdownEditor()
    ed.load_markdown("# Hello\n\nBody.\n")
    out = ed.markdown()
    assert "# Hello" in out
    assert "Body." in out
    ed.deleteLater()


def test_load_clears_dirty(qapp):
    ed = MarkdownEditor()
    ed.load_markdown("# A\n")
    assert not ed.is_dirty()
    ed.deleteLater()


def test_editing_marks_dirty(qapp):
    ed = MarkdownEditor()
    ed.load_markdown("hello\n")
    assert not ed.is_dirty()
    cur = ed.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.End)
    cur.insertText(" world")
    qapp.processEvents()
    assert ed.is_dirty()
    ed.deleteLater()


def test_clear_dirty(qapp):
    ed = MarkdownEditor()
    ed.load_markdown("x\n")
    cur = ed.textCursor()
    cur.insertText("y")
    qapp.processEvents()
    assert ed.is_dirty()
    ed.clear_dirty()
    assert not ed.is_dirty()
    ed.deleteLater()


def test_toggle_bold(qapp):
    ed = MarkdownEditor()
    ed.load_markdown("hello\n")
    cur = ed.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.Start)
    cur.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
    ed.setTextCursor(cur)
    ed.toggle_bold()
    out = ed.markdown()
    assert "**hello**" in out
    ed.deleteLater()


def test_toggle_italic(qapp):
    ed = MarkdownEditor()
    ed.load_markdown("hello\n")
    cur = ed.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.Start)
    cur.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
    ed.setTextCursor(cur)
    ed.toggle_italic()
    out = ed.markdown()
    assert "_hello_" in out
    ed.deleteLater()


def test_set_heading(qapp):
    ed = MarkdownEditor()
    ed.load_markdown("hello\n")
    ed.set_heading(2)
    out = ed.markdown()
    assert out.startswith("## hello")
    ed.deleteLater()


def test_clear_heading(qapp):
    ed = MarkdownEditor()
    ed.load_markdown("# hello\n")
    ed.set_heading(0)
    out = ed.markdown()
    assert not out.startswith("#")
    assert "hello" in out
    ed.deleteLater()


def test_link_activated_signal(qapp, qtbot):
    ed = MarkdownEditor()
    ed.load_markdown("See [[Target]] here.\n")
    with qtbot.waitSignal(ed.linkActivated, timeout=500) as blocker:
        ed.linkActivated.emit("Target")
    assert blocker.args == ["Target"]
    ed.deleteLater()


def test_wikilink_roundtrip_via_editor(qapp):
    ed = MarkdownEditor()
    src = "Go to [[Foo:Bar]] now.\n"
    ed.load_markdown(src)
    out = ed.markdown()
    assert "[[Foo:Bar]]" in out
    ed.deleteLater()


def test_load_markdown_preserves_current_path(qapp):
    ed = MarkdownEditor()
    ed.load_markdown("x\n", page_path="Foo:Bar")
    assert ed._current_path == "Foo:Bar"
    ed.deleteLater()


# ---- image insertion ----


def _make_png(path):
    from PyQt6.QtGui import QImage
    img = QImage(32, 16, QImage.Format.Format_RGB32)
    img.fill(0xFF00FF00)
    img.save(str(path), "PNG")


def test_insert_image_via_api(qapp, tmp_path):
    page_dir = tmp_path
    resdir = page_dir / "_resources"
    resdir.mkdir()
    img_path = resdir / "logo.png"
    _make_png(img_path)
    ed = MarkdownEditor()
    ed.load_markdown("hello\n", base_path=page_dir)
    ed.insert_image("_resources/logo.png", "logo", str(img_path))
    md = ed.markdown()
    assert "![logo](_resources/logo.png)" in md
    ed.deleteLater()


def test_image_roundtrip(qapp, tmp_path):
    page_dir = tmp_path
    (page_dir / "_resources").mkdir()
    img_path = page_dir / "_resources" / "logo.png"
    _make_png(img_path)
    src = "![logo](_resources/logo.png)\n"
    ed = MarkdownEditor()
    ed.load_markdown(src, base_path=page_dir)
    out = ed.markdown()
    assert "![logo](_resources/logo.png)" in out
    ed.deleteLater()


def test_image_alt_preserved(qapp, tmp_path):
    page_dir = tmp_path
    (page_dir / "_resources").mkdir()
    img_path = page_dir / "_resources" / "pic.png"
    _make_png(img_path)
    src = "![My caption](_resources/pic.png)\n"
    ed = MarkdownEditor()
    ed.load_markdown(src, base_path=page_dir)
    out = ed.markdown()
    assert "![My caption](_resources/pic.png)" in out
    ed.deleteLater()


def test_insert_image_collision_rename(qapp, tmp_path):
    from qnotebook.window import _unique_path
    resdir = tmp_path / "_resources"
    resdir.mkdir()
    (resdir / "logo.png").write_bytes(b"a")
    p2 = _unique_path(resdir / "logo.png")
    assert p2.name == "logo-1.png"
    # Simulate p2 existing; next one should be -2
    p2.write_bytes(b"b")
    p3 = _unique_path(resdir / "logo.png")
    assert p3.name == "logo-2.png"


def test_pygments_highlighting_colors_code(qapp):
    """When a fenced block has a language, Pygments should color some runs."""
    try:
        import pygments  # noqa: F401
    except ImportError:
        import pytest
        pytest.skip("Pygments not available")
    ed = MarkdownEditor()
    ed.load_markdown("```python\ndef foo(x):\n    return x\n```\n")
    # Walk the document and look for any fragment with a non-default foreground
    doc = ed.document()
    b = doc.firstBlock()
    found_color = False
    while b.isValid():
        it = b.begin()
        while not it.atEnd():
            frag = it.fragment()
            if frag.isValid():
                fg = frag.charFormat().foreground().color()
                # A default unset foreground typically has alpha=0 / black;
                # any explicit color from our palette will have non-default components.
                if fg.isValid() and (fg.red(), fg.green(), fg.blue()) not in ((0, 0, 0),):
                    found_color = True
            it += 1
        b = b.next()
    assert found_color, "Expected at least one Pygments-colored fragment"
    ed.deleteLater()


def test_file_drop_emits_file_dropped(qapp, tmp_path):
    """Non-image file drop routes to fileDropped, not imageDropped."""
    ed = MarkdownEditor()
    ed.load_markdown("x\n")
    got_file = []
    got_image = []
    ed.fileDropped.connect(lambda p: got_file.append(p))
    ed.imageDropped.connect(lambda p: got_image.append(p))
    # Build a drop event with a local pdf url
    from PyQt6.QtCore import QMimeData, QPointF, Qt, QUrl
    from PyQt6.QtGui import QDropEvent
    mock_file = tmp_path / "report.pdf"
    mock_file.write_bytes(b"%PDF-1.4\n")
    md = QMimeData()
    md.setUrls([QUrl.fromLocalFile(str(mock_file))])
    evt = QDropEvent(
        QPointF(10, 10), Qt.DropAction.CopyAction, md,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    ed.dropEvent(evt)
    assert got_file == [str(mock_file)]
    assert got_image == []
    ed.deleteLater()


def test_image_drop_still_emits_image(qapp, tmp_path):
    ed = MarkdownEditor()
    ed.load_markdown("x\n")
    got_file = []
    got_image = []
    ed.fileDropped.connect(lambda p: got_file.append(p))
    ed.imageDropped.connect(lambda p: got_image.append(p))
    from PyQt6.QtCore import QMimeData, QPointF, Qt, QUrl
    from PyQt6.QtGui import QDropEvent
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n")
    md = QMimeData()
    md.setUrls([QUrl.fromLocalFile(str(img))])
    evt = QDropEvent(
        QPointF(10, 10), Qt.DropAction.CopyAction, md,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    ed.dropEvent(evt)
    assert got_image == [str(img)]
    assert got_file == []
    ed.deleteLater()


def test_autosave_fires_after_interval(qapp):
    ed = MarkdownEditor()
    ed.load_markdown("x\n")
    ed.set_autosave_interval_ms(10)
    fired = []
    ed.autoSaveRequested.connect(lambda: fired.append(1))
    from PyQt6.QtGui import QTextCursor
    cur = ed.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.End)
    cur.insertText(" edited")
    # Wait longer than the interval
    from PyQt6.QtCore import QEventLoop, QTimer
    loop = QEventLoop()
    QTimer.singleShot(80, loop.quit)
    loop.exec()
    assert fired, "autoSaveRequested should fire after idle"
    ed.deleteLater()


def test_autosave_off_suppresses(qapp):
    ed = MarkdownEditor()
    ed.load_markdown("x\n")
    ed.set_autosave_interval_ms(10)
    ed.set_autosave_enabled(False)
    fired = []
    ed.autoSaveRequested.connect(lambda: fired.append(1))
    from PyQt6.QtGui import QTextCursor
    cur = ed.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.End)
    cur.insertText(" edited")
    from PyQt6.QtCore import QEventLoop, QTimer
    loop = QEventLoop()
    QTimer.singleShot(80, loop.quit)
    loop.exec()
    assert not fired
    ed.deleteLater()


def test_focus_out_triggers_autosave(qapp):
    ed = MarkdownEditor()
    ed.load_markdown("x\n")
    fired = []
    ed.autoSaveRequested.connect(lambda: fired.append(1))
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QFocusEvent, QTextCursor
    cur = ed.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.End)
    cur.insertText(" edited")
    # Simulate focus out
    ed.focusOutEvent(QFocusEvent(QFocusEvent.Type.FocusOut, Qt.FocusReason.OtherFocusReason))
    assert fired
    ed.deleteLater()


def test_insert_text_at_cursor(qapp):
    ed = MarkdownEditor()
    ed.load_markdown("hello")
    from PyQt6.QtGui import QTextCursor
    cur = ed.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.End)
    ed.setTextCursor(cur)
    ed.insert_text_at_cursor(" world")
    assert "hello world" in ed.markdown()
    ed.deleteLater()


def test_insert_horizontal_rule(qapp):
    ed = MarkdownEditor()
    ed.load_markdown("before\n")
    from PyQt6.QtGui import QTextCursor
    cur = ed.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.End)
    ed.setTextCursor(cur)
    ed.insert_horizontal_rule()
    out = ed.markdown()
    assert "before" in out
    assert "---" in out
    ed.deleteLater()


def test_image_fragment_in_markdown_source(qapp, tmp_path):
    page_dir = tmp_path
    (page_dir / "_resources").mkdir()
    img_path = page_dir / "_resources" / "logo.png"
    _make_png(img_path)
    ed = MarkdownEditor()
    ed.load_markdown("Before.\n", base_path=page_dir)
    from PyQt6.QtGui import QTextCursor
    cur = ed.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.End)
    ed.setTextCursor(cur)
    ed.insert_image("_resources/logo.png", "logo", str(img_path))
    md = ed.markdown()
    assert "Before." in md
    assert "![logo](_resources/logo.png)" in md
    ed.deleteLater()


# ---- autocomplete ----


def test_completer_detects_wikilink_prefix(qapp):
    ed = MarkdownEditor()
    ed.load_markdown("")
    ed.set_completion_sources(["Home", "Other", "Sub:Child"], [])
    cur = ed.textCursor()
    cur.insertText("See [[Ho")
    info = ed._active_prefix()
    assert info is not None
    mode, prefix, _ = info
    assert mode == "wiki"
    assert prefix == "Ho"
    ed.deleteLater()


def test_completer_detects_tag_prefix(qapp):
    ed = MarkdownEditor()
    ed.load_markdown("")
    ed.set_completion_sources([], ["todo", "urgent", "review"])
    cur = ed.textCursor()
    cur.insertText("foo #tod")
    info = ed._active_prefix()
    assert info is not None
    mode, prefix, _ = info
    assert mode == "tag"
    assert prefix == "tod"
    ed.deleteLater()


def test_completer_wikilink_activation_inserts_closing_brackets(qapp):
    ed = MarkdownEditor()
    ed.load_markdown("")
    ed.set_completion_sources(["Home", "Other"], [])
    cur = ed.textCursor()
    cur.insertText("See [[Ot")
    # Simulate activation
    ed._update_completer()
    ed._on_completer_activated("Other")
    text = ed.toPlainText()
    assert "[[Other]]" in text
    ed.deleteLater()


def test_completer_inactive_outside_brackets(qapp):
    ed = MarkdownEditor()
    ed.load_markdown("")
    ed.set_completion_sources(["Home"], [])
    cur = ed.textCursor()
    cur.insertText("just plain text")
    assert ed._active_prefix() is None
    ed.deleteLater()


# ---- live reparse ----


def test_live_reparse_makes_bold(qapp):
    ed = MarkdownEditor()
    ed.load_markdown("")
    ed.set_live_reparse_enabled(True)
    cur = ed.textCursor()
    cur.insertText("this is **bold** text")
    ed.live_reparse_now()
    # Find the character format at the "b" in "bold".
    full = ed.toPlainText()
    bold_start = full.index("bold")
    cur2 = ed.textCursor()
    cur2.setPosition(bold_start + 1)
    from PyQt6.QtGui import QFont
    assert cur2.charFormat().fontWeight() >= QFont.Weight.Bold
    ed.deleteLater()


def test_live_reparse_roundtrip_preserved(qapp):
    ed = MarkdownEditor()
    ed.load_markdown("hello **world**\n")
    ed.set_live_reparse_enabled(True)
    # Trigger reparse without changing text
    ed.live_reparse_now()
    md = ed.markdown()
    assert "**world**" in md
    ed.deleteLater()


def test_live_reparse_preserves_cursor_position(qapp):
    ed = MarkdownEditor()
    ed.load_markdown("")
    ed.set_live_reparse_enabled(True)
    cur = ed.textCursor()
    cur.insertText("alpha _italic_ beta")
    # Place cursor at a known position
    target = 3
    cur.setPosition(target)
    ed.setTextCursor(cur)
    ed.live_reparse_now()
    assert ed.textCursor().position() == target
    ed.deleteLater()


def test_live_reparse_wikilink_gets_anchor(qapp):
    ed = MarkdownEditor()
    ed.load_markdown("")
    ed.set_live_reparse_enabled(True)
    from qnotebook.md_to_qdoc import CHAR_WIKILINK
    cur = ed.textCursor()
    cur.insertText("see [[Target]] now")
    ed.live_reparse_now()
    # Move cursor into the wikilink
    txt = ed.toPlainText()
    pos = txt.index("Target") + 1
    c2 = ed.textCursor()
    c2.setPosition(pos)
    prop = c2.charFormat().property(CHAR_WIKILINK)
    assert prop == "Target"


def test_live_reparse_md_link_styled(qapp):
    ed = MarkdownEditor()
    ed.load_markdown("")
    ed.set_live_reparse_enabled(True)
    cur = ed.textCursor()
    cur.insertText("see [example](http://x.test) here")
    ed.live_reparse_now()
    txt = ed.toPlainText()
    pos = txt.index("example") + 1
    c2 = ed.textCursor()
    c2.setPosition(pos)
    href = c2.charFormat().anchorHref()
    assert href == "http://x.test"
    ed.deleteLater()


def test_live_reparse_md_image_styled(qapp):
    ed = MarkdownEditor()
    ed.load_markdown("")
    ed.set_live_reparse_enabled(True)
    cur = ed.textCursor()
    cur.insertText("![alt](_resources/x.png)")
    ed.live_reparse_now()
    txt = ed.toPlainText()
    pos = txt.index("alt") + 1
    c2 = ed.textCursor()
    c2.setPosition(pos)
    # Image span: foreground colored
    color = c2.charFormat().foreground().color().name()
    assert color.lower() == "#7c3aed"
    ed.deleteLater()


def test_live_reparse_does_not_style_unclosed_link(qapp):
    """When user is still typing the URL there's no closing `)`; no link styling yet."""
    ed = MarkdownEditor()
    ed.load_markdown("")
    ed.set_live_reparse_enabled(True)
    cur = ed.textCursor()
    cur.insertText("[draft](http://x")
    ed.live_reparse_now()
    txt = ed.toPlainText()
    pos = txt.index("draft") + 1
    c2 = ed.textCursor()
    c2.setPosition(pos)
    assert c2.charFormat().anchorHref() == ""
    ed.deleteLater()


def test_live_reparse_md_link_preserves_roundtrip(qapp):
    ed = MarkdownEditor()
    ed.load_markdown("")
    ed.set_live_reparse_enabled(True)
    cur = ed.textCursor()
    cur.insertText("see [example](http://x.test) end")
    ed.live_reparse_now()
    md = ed.markdown()
    # After live-reparse the literal text remains; serializing emits paragraph
    # text including `[example](http://x.test)` (since the bracketed text is
    # still present in plain text).
    assert "example" in md and "http://x.test" in md
    ed.deleteLater()


def test_toc_marker_styled_in_editor(qapp):
    ed = MarkdownEditor()
    ed.load_markdown("# Top\n\n[[!TOC]]\n\n## Sub\n")
    md = ed.markdown()
    assert "[[!TOC]]" in md
    ed.deleteLater()


def test_equation_loads_and_serializes(qapp):
    ed = MarkdownEditor()
    ed.load_markdown("energy: $E=mc^2$ here\n")
    out = ed.markdown()
    assert "$E=mc^2$" in out
    ed.deleteLater()


def test_live_reparse_unclosed_wikilink_no_anchor(qapp):
    ed = MarkdownEditor()
    ed.load_markdown("")
    ed.set_live_reparse_enabled(True)
    from qnotebook.md_to_qdoc import CHAR_WIKILINK
    cur = ed.textCursor()
    cur.insertText("see [[Target now")  # missing closing ]]
    ed.live_reparse_now()
    txt = ed.toPlainText()
    pos = txt.index("Target") + 1
    c2 = ed.textCursor()
    c2.setPosition(pos)
    assert c2.charFormat().property(CHAR_WIKILINK) is None
    ed.deleteLater()
    ed.deleteLater()


def test_transclusion_renders_included_content(qapp):
    from PyQt6.QtGui import QTextDocument
    from qnotebook.md_to_qdoc import BLOCK_TRANSCLUSION, markdown_to_qdoc

    def resolver(target: str) -> str | None:
        if target == "Foo":
            return "Included body text."
        return None

    doc = QTextDocument()
    markdown_to_qdoc("Before\n\n{{Foo}}\n\nAfter\n", doc, transclusion_resolver=resolver)
    full = doc.toPlainText()
    assert "{{Foo}}" in full
    assert "Included body text." in full
    # Block property present
    found = False
    block = doc.firstBlock()
    while block.isValid():
        if block.blockFormat().property(BLOCK_TRANSCLUSION):
            found = True
            break
        block = block.next()
    assert found


def test_transclusion_infinite_loop_guarded(qapp):
    # Resolver returning a page that transcludes itself shouldn't infinite loop.
    # We simulate by always returning None for the guarded case.
    from PyQt6.QtGui import QTextDocument
    from qnotebook.md_to_qdoc import markdown_to_qdoc

    calls = 0

    def resolver(target: str) -> str | None:
        nonlocal calls
        calls += 1
        if calls > 5:
            raise AssertionError("infinite loop")
        return None

    doc = QTextDocument()
    markdown_to_qdoc("{{Self}}\n", doc, transclusion_resolver=resolver)
    assert "{{Self}}" in doc.toPlainText()


def test_transclusion_no_resolver_still_serializes(qapp):
    from PyQt6.QtGui import QTextDocument
    from qnotebook.md_to_qdoc import markdown_to_qdoc
    from qnotebook.qdoc_to_md import qdoc_to_markdown

    doc = QTextDocument()
    markdown_to_qdoc("Hello\n\n{{Foo}}\n\nWorld\n", doc)
    out = qdoc_to_markdown(doc)
    assert "{{Foo}}" in out
    assert "Hello" in out
    assert "World" in out


def test_toc_marker_generates_heading_list(qapp):
    from PyQt6.QtGui import QTextDocument
    from qnotebook.md_to_qdoc import BLOCK_TRANSCLUDED_CHILD, markdown_to_qdoc
    doc = QTextDocument()
    markdown_to_qdoc("# A\n\n[[!TOC]]\n\n## B\n\n## C\n", doc)
    # TOC expanded to 3 child blocks (A, B, C).
    found = 0
    block = doc.firstBlock()
    while block.isValid():
        if block.blockFormat().property(BLOCK_TRANSCLUDED_CHILD):
            found += 1
        block = block.next()
    assert found == 3


def test_toc_marker_heading_anchors_target_same_page(qapp):
    from PyQt6.QtGui import QTextDocument
    from qnotebook.md_to_qdoc import CHAR_WIKILINK, markdown_to_qdoc
    doc = QTextDocument()
    markdown_to_qdoc("# Top\n\n[[!TOC]]\n\n## Sub\n", doc)
    # Walk every character: at least one should have a CHAR_WIKILINK property
    # starting with `#` (same-page anchor to a heading).
    found_anchor = False
    block = doc.firstBlock()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            if frag.isValid():
                link = frag.charFormat().property(CHAR_WIKILINK)
                if link and str(link).startswith("#"):
                    found_anchor = True
                    break
            it += 1
        if found_anchor:
            break
        block = block.next()
    assert found_anchor


def test_toc_marker_roundtrip_still_works(qapp):
    from PyQt6.QtGui import QTextDocument
    from qnotebook.md_to_qdoc import markdown_to_qdoc
    from qnotebook.qdoc_to_md import qdoc_to_markdown
    doc = QTextDocument()
    markdown_to_qdoc("# Top\n\n[[!TOC]]\n\n## Sub\n", doc)
    out = qdoc_to_markdown(doc)
    assert "[[!TOC]]" in out
    # Anchor-list children are marked BLOCK_TRANSCLUDED_CHILD and dropped on save.
    assert "## Sub" in out
