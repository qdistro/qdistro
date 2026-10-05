"""Plan 06 content invariants under presentation changes (09 item 14).

- Markdown syntax never follows rendered font size: a body font below and
  above the old 11.5 pt bold heuristic serializes identically.
- Real undo/redo across several live theme changes restores the authored
  text, with the modified state following the undo stack.
- PDF export is the explicit export baseline: the same bytes (modulo PDF
  metadata) whatever the live shell mode or document-font opt-in.
"""

from __future__ import annotations

import re
from dataclasses import replace

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtGui import QTextCursor, QTextDocument
from qdistro_presentation.model import example_snapshot, with_generation
from qdistro_presentation.paths import ENV_OVERRIDE
from qdistro_presentation.publish import write_snapshot
from qnotebook.appearance import (
    SettingsAdapter,
    save_theme_mode,
    save_use_desktop_document_fonts,
)
from qnotebook.content_style import (
    ContentStyle,
    apply_document_presentation,
    legacy_content_style,
)
from qnotebook.editor import reset_pinned_body_font_for_tests
from qnotebook.export import export_page_pdf
from qnotebook.md_to_qdoc import markdown_to_qdoc
from qnotebook.qdoc_to_md import qdoc_to_markdown
from qnotebook.theme import (
    attach_presentation,
    current_controller,
    reset_controller_for_tests,
)
from qnotebook.window import MainWindow

SAMPLE = (
    "# Heading with **bold** and *italic*\n"
    "\n"
    "###### Small heading `code`\n"
    "\n"
    "Body **strong** and *em* and `inline code` and [a link](https://example.org).\n"
    "\n"
    "> quoted line\n"
    "\n"
    "```python\n"
    "print('x')\n"
    "```\n"
    "\n"
    "- item one\n"
    "- item two\n"
)


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


def _attach(qapp, tmp_path, monkeypatch, snap):
    write_snapshot(str(tmp_path), snap, require_unwritable_dirs=False, skip_unchanged=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    s = QSettings("qnotebook", "qnotebook")
    save_theme_mode(s, "system", update_legacy=False)
    attach_presentation(qapp, SettingsAdapter(s))


def _publish(tmp_path, snap):
    write_snapshot(str(tmp_path), snap, require_unwritable_dirs=False, skip_unchanged=False)
    ctrl = current_controller()
    assert ctrl is not None
    ctrl._reload()  # the file watch is asynchronous; drive it for the test


@pytest.mark.parametrize("body_pt", [9.0, 10.5, 11.4, 11.6, 12.0, 16.0])
def test_serialization_ignores_body_font_size(qapp, body_pt):
    base_doc = QTextDocument()
    markdown_to_qdoc(SAMPLE, base_doc)
    baseline = qdoc_to_markdown(base_doc)
    doc = QTextDocument()
    markdown_to_qdoc(SAMPLE, doc)
    style = replace(
        legacy_content_style(),
        body_point_size=body_pt,
        code_point_size=body_pt,
        inherit_desktop=True,
    )
    assert isinstance(style, ContentStyle)
    apply_document_presentation(doc, style)
    assert qdoc_to_markdown(doc) == baseline


def test_undo_redo_across_theme_changes(qapp, tmp_path, tmp_notebook, qtbot, monkeypatch):
    dark = example_snapshot()
    light = with_generation(replace(dark, mode="light"))
    _attach(qapp, tmp_path, monkeypatch, dark)
    save_use_desktop_document_fonts(QSettings("qnotebook", "qnotebook"), True)
    win = MainWindow()
    win.open_notebook(str(tmp_notebook))
    qtbot.addWidget(win)
    ed = win.editor
    ed.load_markdown(SAMPLE, page_path=win._current_page)
    ed.clear_dirty()
    authored = ed.markdown()

    ed.insert_text_at_cursor("EDIT1 ")
    after_one = ed.markdown()
    for snap in (light, dark, light):
        _publish(tmp_path, snap)
        win.apply_presentation_update()
        assert ed.markdown() == after_one
    # Not adjacent to EDIT1: Qt merges contiguous typing into one undo step.
    cur = ed.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.Start)
    ed.setTextCursor(cur)
    ed.insert_text_at_cursor("EDIT2 ")
    after_two = ed.markdown()
    _publish(tmp_path, dark)
    win.apply_presentation_update()

    ed.undo()
    assert ed.markdown() == after_one
    ed.undo()
    assert ed.markdown() == authored
    assert not ed.is_dirty()
    _publish(tmp_path, light)
    win.apply_presentation_update()
    assert ed.markdown() == authored
    ed.redo()
    assert ed.markdown() == after_one
    assert ed.is_dirty()
    ed.redo()
    assert ed.markdown() == after_two
    win.close()


_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)


def _pdf_body(path) -> list[bytes]:
    """Page content, font and image streams plus the page count. The XMP
    metadata stream (random document UUIDs, timestamps) is excluded."""
    data = path.read_bytes()
    assert data.startswith(b"%PDF")
    streams = [m.group(1) for m in _STREAM.finditer(data) if b"x:xmpmeta" not in m.group(1)]
    pages = len(re.findall(rb"/Type\s*/Page\b", data))
    return [str(pages).encode()] + streams


def test_pdf_export_matches_baseline_in_any_live_mode(qapp, tmp_path, tmp_notebook, qtbot, monkeypatch):
    snap_dir = tmp_path / "snap"
    snap_dir.mkdir()
    dark = example_snapshot()
    light = with_generation(replace(dark, mode="light"))
    win = MainWindow()  # no controller: the legacy/native baseline
    win.open_notebook(str(tmp_notebook))
    qtbot.addWidget(win)
    page = win._current_page
    base_pdf = tmp_path / "base.pdf"
    export_page_pdf(win.notebook, page, base_pdf)
    win.close()

    _attach(qapp, snap_dir, monkeypatch, dark)
    save_use_desktop_document_fonts(QSettings("qnotebook", "qnotebook"), True)
    outputs = []
    for name, snap in (("dark", dark), ("light", light)):
        _publish(snap_dir, snap)
        win = MainWindow()
        win.open_notebook(str(tmp_notebook))
        qtbot.addWidget(win)
        win.apply_presentation_update()
        out = tmp_path / f"{name}.pdf"
        export_page_pdf(win.notebook, win._current_page, out)
        outputs.append(out)
        win.close()
    base = _pdf_body(base_pdf)
    assert int(base[0]) >= 1 and len(base) > 2
    for out in outputs:
        assert _pdf_body(out) == base, out.name

    # Sensitivity: the comparison must see a real content change.
    reset_controller_for_tests()
    win = MainWindow()
    win.open_notebook(str(tmp_notebook))
    qtbot.addWidget(win)
    page_file = win.notebook.file_for(win._current_page)
    page_file.write_text(page_file.read_text() + "\nAn extra paragraph.\n")
    changed = tmp_path / "changed.pdf"
    export_page_pdf(win.notebook, win._current_page, changed)
    win.close()
    assert _pdf_body(changed) != base
