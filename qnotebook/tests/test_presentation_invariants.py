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
from PyQt6.QtGui import QTextCharFormat, QTextCursor, QTextDocument
from qdistro_presentation.model import example_snapshot, with_generation
from qdistro_presentation.paths import ENV_OVERRIDE
from qdistro_presentation.publish import write_snapshot
from qnotebook.appearance import (
    SettingsAdapter,
    save_theme_mode,
    save_use_desktop_document_fonts,
)
from qnotebook.content_style import (
    apply_document_presentation,
    desktop_content_style,
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


def _snap(mode, ui_family, fixed_family, ui_scale):
    base = example_snapshot()
    fonts = replace(base.fonts, ui_family=ui_family, fixed_family=fixed_family, ui_scale=ui_scale)
    return with_generation(replace(base, mode=mode, fonts=fonts))




def _snaps():
    # Both differ from the legacy content family (DejaVu Serif on this
    # stack) and from each other, in family, fixed family and size.
    a = _snap("dark", "DejaVu Sans", "DejaVu Sans Mono", 1.0)
    b = _snap("light", "Bitstream Vera Serif", "JetBrains Mono", 1.2)
    return a, b


def _body_fragment_format(ed):
    """Rendered format inside the first plain body paragraph of SAMPLE.

    A live restyle paints through the content highlighter (QTextLayout
    overlay formats) without touching char formats, so read the overlay
    covering the position and fall back to the stored char format.
    """
    text = ed.document().toPlainText()
    pos = text.index("Body ") + 1
    block = ed.document().findBlock(pos)
    rel = pos - block.position()
    c = QTextCursor(ed.document())
    c.setPosition(pos)
    fmt = QTextCharFormat(c.charFormat())
    for rng in block.layout().formats():
        if rng.start <= rel < rng.start + rng.length:
            fmt.merge(rng.format)
    return fmt


def _publish_and_wait(qtbot, tmp_path, ed, snap):
    """Publish through the real file watcher and wait until the editor's
    document shows the new resolved body family and size."""
    write_snapshot(str(tmp_path), snap, require_unwritable_dirs=False, skip_unchanged=False)
    ctrl = current_controller()
    assert ctrl is not None
    qtbot.waitUntil(lambda: ctrl.state.generation == snap.generation, timeout=5000)
    want = desktop_content_style()
    assert want.body_family == snap.fonts.ui_family

    def applied():
        fmt = _body_fragment_format(ed)
        fams = list(fmt.fontFamilies() or []) + [fmt.fontFamily()]
        return want.body_family in fams and abs(fmt.fontPointSize() - want.body_point_size) < 0.05

    qtbot.waitUntil(applied, timeout=5000)
    return want


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
    apply_document_presentation(doc, style)
    # The restyle really applied the sizes on both sides of the old 11.5 pt
    # bold/heading heuristic ...
    text = doc.toPlainText()
    c = QTextCursor(doc)
    c.setPosition(text.index("Body ") + 1)
    assert abs(c.charFormat().fontPointSize() - body_pt) < 0.05
    c.setPosition(text.index("Heading") + 1)
    assert abs(c.charFormat().fontPointSize() - style.heading_point_size(1)) < 0.05
    # ... and Markdown syntax did not follow them.
    out = qdoc_to_markdown(doc)
    assert out == baseline
    assert out.startswith("# Heading with **bold** and _italic_\n")
    assert "Body **strong** and _em_" in out


def test_undo_redo_across_theme_changes(qapp, tmp_path, tmp_notebook, qtbot, monkeypatch):
    snap_a, snap_b = _snaps()
    _attach(qapp, tmp_path, monkeypatch, snap_a)
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
    for snap in (snap_b, snap_a, snap_b):
        _publish_and_wait(qtbot, tmp_path, ed, snap)
        assert ed.markdown() == after_one
    # Not adjacent to EDIT1: Qt merges contiguous typing into one undo step.
    cur = ed.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.Start)
    ed.setTextCursor(cur)
    ed.insert_text_at_cursor("EDIT2 ")
    after_two = ed.markdown()
    _publish_and_wait(qtbot, tmp_path, ed, snap_a)

    ed.undo()
    assert ed.markdown() == after_one
    ed.undo()
    assert ed.markdown() == authored
    assert not ed.is_dirty()
    _publish_and_wait(qtbot, tmp_path, ed, snap_b)
    assert ed.markdown() == authored
    assert not ed.is_dirty()
    ed.redo()
    assert ed.markdown() == after_one
    assert ed.is_dirty()
    ed.redo()
    assert ed.markdown() == after_two
    win.close()


# Only genuinely nondeterministic PDF metadata is normalized, each pattern
# anchored to its own key; everything else (page tree, boxes, resources,
# content, fonts, xref offsets, any other hex or date string) is compared.
_ZERO_FILL = lambda m: m.group(1) + b"0" * len(m.group(2)) + m.group(3)  # noqa: E731
_PDF_NORMALIZE = (
    # Trailer /ID [<hex> <hex>]: hex of a random UUID string; keep the length
    # so xref offsets still line up.
    (re.compile(rb"(\ntrailer\n<<.*?/ID \[ <)([0-9a-fA-F]+)(> <)", re.S), _ZERO_FILL),
    (re.compile(rb"(\ntrailer\n<<.*?/ID \[ <[0-9a-fA-F]+> <)([0-9a-fA-F]+)(>)", re.S), _ZERO_FILL),
    (re.compile(rb'(xmpMM:(?:Document|Instance)ID="uuid:)([0-9a-f-]{36})(")'), _ZERO_FILL),
    (re.compile(rb'(xmp:(?:Create|Modify|Metadata)Date=")(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:Z|[+-]\d\d:\d\d))(")'), _ZERO_FILL),
    (re.compile(rb"(/(?:CreationDate|ModDate) \(D:)(\d{14}(?:Z|[+-]\d\d'\d\d'))(\))"), _ZERO_FILL),
)


def _normalize_pdf_bytes(data: bytes) -> bytes:
    for pat, repl in _PDF_NORMALIZE:
        data = pat.sub(repl, data)
    return data


def _pdf_normalized(path) -> bytes:
    data = path.read_bytes()
    assert data.startswith(b"%PDF")
    return _normalize_pdf_bytes(data)


@pytest.mark.parametrize("pdf_zone,xmp_zone", [(b"+02'00'", b"+02:00"), (b"-05'30'", b"-05:30"), (b"Z", b"Z")])
def test_pdf_normalization_is_scoped_to_volatile_metadata(pdf_zone, xmp_zone):
    def pdf(trailer_id: bytes, other_hex: bytes, other_date: bytes, meta_date: bytes) -> bytes:
        return (
            b"%PDF-1.4\n1 0 obj\n<<\n/CreationDate (D:" + meta_date + pdf_zone + b")\n/ModDate (D:" + meta_date + pdf_zone + b")\n>>\n"
            b'<x xmp:CreateDate="' + meta_date[:4] + b"-" + meta_date[4:6] + b"-" + meta_date[6:8]
            + b"T" + meta_date[8:10] + b":" + meta_date[10:12] + b":" + meta_date[12:14] + xmp_zone + b'" xmpMM:DocumentID="uuid:'
            + trailer_id[:8] + b'-b04a-4d74-9c30-d29c652323f3"/>\n'
            b"2 0 obj\n<< /Font <" + other_hex + b"> /Note (D:" + other_date + b"+02'00') >>\n"
            b"trailer\n<<\n/Size 3 \n/ID [ <" + trailer_id + b"> <" + trailer_id + b"> ]\n>>\n%%EOF\n"
        )

    base = pdf(b"37" * 36, b"ab" * 20, b"20261005103409", b"20261005103409")
    norm = _normalize_pdf_bytes(base)
    # Volatile metadata alone may differ.
    assert _normalize_pdf_bytes(pdf(b"38" * 36, b"ab" * 20, b"20261005103409", b"20270101000000")) == norm
    # A long hex string or a date anywhere else is real content.
    assert _normalize_pdf_bytes(pdf(b"37" * 36, b"ac" * 20, b"20261005103409", b"20261005103409")) != norm
    assert _normalize_pdf_bytes(pdf(b"37" * 36, b"ab" * 20, b"20270101000000", b"20261005103409")) != norm
    # Length is preserved, so xref offsets keep lining up.
    assert len(norm) == len(base)


def _export(tmp_notebook, qtbot, out):
    win = MainWindow()
    win.open_notebook(str(tmp_notebook))
    qtbot.addWidget(win)
    export_page_pdf(win.notebook, win._current_page, out)
    return win


def test_pdf_export_matches_baseline_in_any_live_mode(qapp, tmp_path, tmp_notebook, qtbot, monkeypatch):
    snap_dir = tmp_path / "snap"
    snap_dir.mkdir()
    snap_a, snap_b = _snaps()
    base_pdf = tmp_path / "base.pdf"
    _export(tmp_notebook, qtbot, base_pdf).close()  # no controller: baseline
    base = _pdf_normalized(base_pdf)

    _attach(qapp, snap_dir, monkeypatch, snap_a)
    save_use_desktop_document_fonts(QSettings("qnotebook", "qnotebook"), True)
    win = MainWindow()
    win.open_notebook(str(tmp_notebook))
    qtbot.addWidget(win)
    ed = win.editor
    ed.load_markdown(SAMPLE, page_path=win._current_page)
    live_families = []
    for name, snap in (("a", snap_a), ("b", snap_b)):
        if name == "b":
            _publish_and_wait(qtbot, snap_dir, ed, snap)
        # The live document typography is really the snapshot's, not legacy.
        fmt = _body_fragment_format(ed)
        fams = list(fmt.fontFamilies() or []) + [fmt.fontFamily()]
        assert snap.fonts.ui_family in fams
        live_families.append(snap.fonts.ui_family)
        out = tmp_path / f"{name}.pdf"
        export_page_pdf(win.notebook, win._current_page, out)
        assert _pdf_normalized(out) == base, name
    win.close()
    # The live document typography really changed between the two exports.
    assert live_families[0] != live_families[1]

    # Sensitivity: a real content change is visible to the comparison.
    reset_controller_for_tests()
    win = MainWindow()
    win.open_notebook(str(tmp_notebook))
    qtbot.addWidget(win)
    page_file = win.notebook.file_for(win._current_page)
    page_file.write_text(page_file.read_text() + "\nAn extra paragraph.\n")
    changed = tmp_path / "changed.pdf"
    export_page_pdf(win.notebook, win._current_page, changed)
    win.close()
    assert _pdf_normalized(changed) != base
